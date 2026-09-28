"""``hr/leave-types/`` (module ``hr_setup``) and ``hr/leave/`` (module ``leave``: view / create / approve).

Leave records (PLAN §3.4 "Staff self-service → PENDING; HR → APPROVED"):

* the employee must be within the caller's ``leave`` scope (``self``: their own record — the default when
  ``employee`` is omitted; ``office``: their office; ``all``) — else 400 ``employee_not_found``; a caller without a
  linked employee cannot file for themselves (403 ``employee_not_linked``); inactive employees get none
  (409 ``employee_inactive``);
* the new record is APPROVED when the caller may approve it (``leave.approve`` and it is not their own — the
  ``deny_self_action`` rule) or the type needs no approval; otherwise PENDING;
* dates are ordered, a half day is a single date, one request spans at most 366 days (400); a request may not
  overlap another PENDING or APPROVED one of the same employee (409 ``leave_overlap``, checked under a row lock on
  the employee);
* ``approve/`` and ``reject/`` (``leave.approve``) act on PENDING only (409 ``leave_not_pending``) and never on the
  caller's own leave (403 ``self_action_denied``); ``cancel/`` (``leave.create``): the employee withdraws their own
  PENDING leave, or APPROVED leave that has not started (409 ``leave_already_started``); an approver cancels anyone's
  PENDING or APPROVED leave; REJECTED/CANCELLED cannot be cancelled (409 ``leave_not_cancellable``);
* approved leave (created approved, approved, or cancelled after approval) re-computes those days.

Leave types: codes unique among live types, case-insensitive (409 ``leave_type_code_taken``); a type used by any
leave record cannot be deleted (409 ``leave_type_in_use``).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from accounts.services.authz import can, deny_self_action, is_self
from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, PermissionDenied
from core.services import stamp_create
from flarize.cache_utils import bump
from hr.models import Employee, LeaveRecord, LeaveType
from hr.services import recompute, validation
from hr.services.common import NS_LEAVE, bump_setup, lock, now, today_in, unique_conflict
from hr.services.scopes import employees_for_module, own_employee

MAX_SPAN_DAYS = 366
OPEN_STATUSES = (LeaveRecord.Status.PENDING, LeaveRecord.Status.APPROVED)
TYPE_FIELDS = ("code", "name", "paid", "requires_approval")
TYPE_UNIQUE = {"hr_leave_type_code_live_uniq": ("leave_type_code_taken", "code", "Another leave type already uses this code.")}
LEAVE_SNAPSHOT = ("employee", "leave_type", "date_from", "date_to", "is_half_day", "status", "reason", "decided_by", "decided_at")


# ----------------------------------------------------------------------------------------------------------------
# Leave types
# ----------------------------------------------------------------------------------------------------------------
def leave_types_queryset():
    return LeaveType.objects.annotate(record_count=Count("leave_records", filter=Q(leave_records__deleted_at__isnull=True), distinct=True)).order_by("name", "id")


def _type_values(data) -> dict:
    values = {name: data[name] for name in TYPE_FIELDS if name in data}
    if "code" in values:
        values["code"] = validation.code(values["code"], max_length=40)
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise validation.invalid("name", "This field may not be blank.")
    return values


def _save_type(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, TYPE_UNIQUE) or exc) from None


@transaction.atomic
def create_leave_type(*, user, data) -> LeaveType:
    leave_type = LeaveType(**_type_values(data))
    stamp_create(leave_type, user)
    _save_type(leave_type.save)
    record("hr.leave_type_created", obj=leave_type, actor=user, after=snapshot(leave_type, TYPE_FIELDS))
    bump_setup()
    return leave_type


@transaction.atomic
def update_leave_type(instance: LeaveType, *, user, data, expected_version=None) -> LeaveType:
    leave_type = lock(LeaveType, instance, expected_version)
    before = snapshot(leave_type, TYPE_FIELDS)
    values = {name: value for name, value in _type_values(data).items() if getattr(leave_type, name) != value}
    if not values:
        return leave_type
    _save_type(lambda: leave_type.versioned_update(user, **values))
    changed_before, changed_after = changes(before, snapshot(leave_type, TYPE_FIELDS))
    record("hr.leave_type_updated", obj=leave_type, actor=user, before=changed_before, after=changed_after)
    bump_setup()
    return leave_type


@transaction.atomic
def delete_leave_type(instance: LeaveType, *, user, expected_version=None) -> None:
    leave_type = lock(LeaveType, instance, expected_version)
    used = LeaveRecord.all_objects.filter(leave_type=leave_type).count()
    if used:
        raise Conflict("leave_type_in_use", f"{used} leave record(s) use '{leave_type.name}'.", errors={"leave_records": [str(used)]})
    leave_type.soft_delete(user)
    record("hr.leave_type_deleted", obj=leave_type, actor=user, before=snapshot(leave_type, TYPE_FIELDS))
    bump_setup()


# ----------------------------------------------------------------------------------------------------------------
# Leave records
# ----------------------------------------------------------------------------------------------------------------
def leave_queryset():
    return LeaveRecord.objects.filter(employee__deleted_at__isnull=True).select_related("employee__office", "employee__user", "leave_type", "decided_by").order_by("-date_from", "-id")


def leave_snapshot(leave: LeaveRecord) -> dict:
    return snapshot(leave, LEAVE_SNAPSHOT)


def _resolve_employee(user, employee: Employee | None) -> Employee:
    if employee is None:
        employee = own_employee(user)
        if employee is None:
            raise PermissionDenied("employee_not_linked", "Your login is not linked to an employee record; choose the employee.")
    if not employees_for_module(user, "leave").filter(pk=employee.pk).exists():
        raise DomainError("employee_not_found", "You cannot file leave for this employee.", errors={"employee": ["Not found."]})
    return employee


def _check_dates(date_from, date_to, is_half_day: bool) -> None:
    if date_to < date_from:
        raise validation.invalid("date_to", "date_to cannot be before date_from.")
    if is_half_day and date_to != date_from:
        raise validation.invalid("is_half_day", "A half day covers a single date (date_from = date_to).")
    if (date_to - date_from).days + 1 > MAX_SPAN_DAYS:
        raise validation.invalid("date_to", f"One request covers at most {MAX_SPAN_DAYS} days.")


def _check_overlap(employee: Employee, date_from, date_to, *, exclude_pk=None, statuses=OPEN_STATUSES) -> None:
    clash = LeaveRecord.objects.filter(employee=employee, status__in=statuses, date_from__lte=date_to, date_to__gte=date_from)
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    other = clash.order_by("date_from").first()
    if other is not None:
        raise Conflict("leave_overlap", f"Overlaps {other.status.lower()} leave from {other.date_from} to {other.date_to}.", errors={"date_from": [str(other.uid)]})


def _recompute(leave: LeaveRecord, reason: str) -> None:
    recompute.for_employees([leave.employee.uid], leave.date_from, leave.date_to, reason=reason)


@transaction.atomic
def create_leave(*, user, data) -> LeaveRecord:
    employee = _resolve_employee(user, data.get("employee"))
    employee = Employee.objects.select_for_update(of=("self",)).get(pk=employee.pk)  # serialises overlap checks per employee
    if not employee.is_active:
        raise Conflict("employee_inactive", "This employee is deactivated.")
    date_from, date_to, is_half_day = data["date_from"], data["date_to"], bool(data.get("is_half_day", False))
    _check_dates(date_from, date_to, is_half_day)
    _check_overlap(employee, date_from, date_to)
    leave_type = data["leave_type"]
    approved = not leave_type.requires_approval or (can(user, "leave", "approve") and not is_self(user, employee))
    leave = LeaveRecord(
        employee=employee,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
        is_half_day=is_half_day,
        reason=(data.get("reason") or "").strip(),
        status=LeaveRecord.Status.APPROVED if approved else LeaveRecord.Status.PENDING,
    )
    if approved:
        leave.decided_by, leave.decided_at = user, now()
    stamp_create(leave, user)
    leave.save()
    record("hr.leave_created", obj=leave, actor=user, after=leave_snapshot(leave))
    if approved:
        _recompute(leave, "leave_approved")
    bump(NS_LEAVE)
    return leave


def _decide(instance: LeaveRecord, *, user, status: str, expected_version, note: str) -> LeaveRecord:
    leave = lock(LeaveRecord, instance, expected_version, related=("employee__user",))
    if leave.status != LeaveRecord.Status.PENDING:
        raise Conflict("leave_not_pending", f"Only pending leave can be decided (this one is {leave.status}).")
    deny_self_action(user, leave, module="leave", action="approve", message="You cannot decide your own leave.")
    if status == LeaveRecord.Status.APPROVED:
        _check_overlap(leave.employee, leave.date_from, leave.date_to, exclude_pk=leave.pk, statuses=(LeaveRecord.Status.APPROVED,))
    before = leave_snapshot(leave)
    leave.versioned_update(user, status=status, decided_by=user, decided_at=now())
    changed_before, changed_after = changes(before, leave_snapshot(leave))
    record(f"hr.leave_{status.lower()}", obj=leave, actor=user, before=changed_before, after=changed_after, note=note)
    if status == LeaveRecord.Status.APPROVED:
        _recompute(leave, "leave_approved")
    bump(NS_LEAVE)
    return leave


@transaction.atomic
def approve_leave(instance: LeaveRecord, *, user, expected_version=None, note: str = "") -> LeaveRecord:
    return _decide(instance, user=user, status=LeaveRecord.Status.APPROVED, expected_version=expected_version, note=note)


@transaction.atomic
def reject_leave(instance: LeaveRecord, *, user, expected_version=None, note: str = "") -> LeaveRecord:
    return _decide(instance, user=user, status=LeaveRecord.Status.REJECTED, expected_version=expected_version, note=note)


@transaction.atomic
def cancel_leave(instance: LeaveRecord, *, user, expected_version=None, note: str = "") -> LeaveRecord:
    leave = lock(LeaveRecord, instance, expected_version, related=("employee__user", "employee__office"))
    if leave.status not in OPEN_STATUSES:
        raise Conflict("leave_not_cancellable", f"{leave.status.capitalize()} leave cannot be cancelled.")
    if is_self(user, leave):
        today = today_in(leave.employee.office.timezone if leave.employee.office_id else None)
        if leave.status == LeaveRecord.Status.APPROVED and leave.date_from <= today:
            raise Conflict("leave_already_started", "Approved leave that has started can only be cancelled by an approver.")
    elif not can(user, "leave", "approve"):
        raise PermissionDenied("permission_denied", "Only an approver can cancel someone else's leave.")
    was_approved = leave.status == LeaveRecord.Status.APPROVED
    before = leave_snapshot(leave)
    leave.versioned_update(user, status=LeaveRecord.Status.CANCELLED, decided_by=user, decided_at=now())
    changed_before, changed_after = changes(before, leave_snapshot(leave))
    record("hr.leave_cancelled", obj=leave, actor=user, before=changed_before, after=changed_after, note=note)
    if was_approved:
        _recompute(leave, "leave_cancelled")
    bump(NS_LEAVE)
    return leave
