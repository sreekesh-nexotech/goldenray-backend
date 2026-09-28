"""``hr/employees/`` — employees (module ``employees``: view / create / edit / archive).

* codes are unique among live employees, case-insensitive (409 ``employee_code_taken``); phones are stored as E.164
  (Indian numbers without a country code are accepted); ``left_on`` cannot precede ``joined_on``;
* ``is_active`` changes only through ``deactivate/`` and ``activate/`` (archive). Deactivating emits
  ``hr.employee_deactivated`` ``{"employee_uid", "user_uid" | null}`` — accounts then deactivates the linked login and
  ends its sessions. Nobody deactivates their own employee record, nor one whose login they could not manage
  themselves (the accounts handler acts as SYSTEM and cannot re-check). Activating does not restore the login
  (eSSL behaviour): re-link it (``link-user/``) or reactivate the account in ``users/``;
* an employee with any history (leave records here; attendance days, raw punches, device mappings registered by the
  later packages) cannot be deleted (409 ``employee_has_history``): deactivate instead. Deleting one also retires its
  login like a deactivation;
* office, shift and date changes re-compute the recent attendance of that employee (``hr.attendance_inputs_changed``).

Login link and photo: :mod:`hr.services.employee_links`.
"""

from __future__ import annotations

from datetime import date

from django.db import IntegrityError, transaction

from accounts.services.authz import deny_self_action
from accounts.services.users import ensure_can_manage_user
from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import stamp_create
from flarize.cache_utils import bump
from hr import registries
from hr.models import Employee, LeaveRecord
from hr.services import recompute, validation
from hr.services.common import NS_EMPLOYEES, lock, unique_conflict

EDITABLE_FIELDS = ("code", "full_name", "office", "shift", "department", "designation", "email", "phone_e164", "joined_on", "left_on", "identity_method")
SNAPSHOT_FIELDS = (*EDITABLE_FIELDS, "is_active", "user", "photo")
RECOMPUTE_FIELDS = ("office", "shift", "joined_on", "left_on")
UNIQUE = {"hr_employee_code_live_uniq": ("employee_code_taken", "code", "Another employee already uses this code.")}
DEACTIVATED_EVENT = "hr.employee_deactivated"


def employees_queryset():
    return Employee.objects.select_related("office__default_shift", "shift", "user__role", "photo").order_by("full_name", "id")


def employee_snapshot(employee: Employee) -> dict:
    return snapshot(employee, SNAPSHOT_FIELDS)


def _normalise(values: dict) -> dict:
    if "code" in values:
        values["code"] = validation.code(values["code"], max_length=50)
    if "full_name" in values:
        values["full_name"] = (values["full_name"] or "").strip()
        if not values["full_name"]:
            raise validation.invalid("full_name", "This field may not be blank.")
    for name in ("department", "designation"):
        if name in values:
            values[name] = (values[name] or "").strip()
    if "email" in values:
        values["email"] = validation.email(values["email"])
    if "phone_e164" in values:
        values["phone_e164"] = validation.phone_e164(values["phone_e164"])
    return values


def _check_dates(joined_on: date | None, left_on: date | None) -> None:
    if joined_on and left_on and left_on < joined_on:
        raise validation.invalid("left_on", "left_on cannot be before joined_on.")


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


def _bump() -> None:
    bump(NS_EMPLOYEES)


@transaction.atomic
def create_employee(*, user, data) -> Employee:
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    _check_dates(values.get("joined_on"), values.get("left_on"))
    employee = Employee(**values)
    stamp_create(employee, user)
    _save(employee.save)
    record("hr.employee_created", obj=employee, actor=user, after=employee_snapshot(employee))
    _bump()
    return employee


@transaction.atomic
def update_employee(instance: Employee, *, user, data, expected_version=None) -> Employee:
    employee = lock(Employee, instance, expected_version)
    before = employee_snapshot(employee)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(employee, name) != value}
    if not values:
        return employee
    _check_dates(values.get("joined_on", employee.joined_on), values.get("left_on", employee.left_on))
    old_dates = [employee.joined_on, employee.left_on]
    _save(lambda: employee.versioned_update(user, **values))
    changed_before, changed_after = changes(before, employee_snapshot(employee))
    record("hr.employee_updated", obj=employee, actor=user, before=changed_before, after=changed_after)
    if set(values) & set(RECOMPUTE_FIELDS):
        start, today = recompute.recent_range()
        dates = [day for day in [*old_dates, employee.joined_on, employee.left_on, start] if day is not None and day <= today]
        recompute.for_employees([employee.uid], min(dates), today, reason="employee_updated")
    _bump()
    return employee


def _guard_account(actor, employee: Employee, *, message: str) -> None:
    """Nobody retires their own record; nobody retires a record whose login they could not manage themselves."""
    deny_self_action(actor, employee, message=message)
    if employee.user is not None:
        ensure_can_manage_user(actor, employee.user)


def _announce_deactivation(employee: Employee) -> None:
    emit(
        DEACTIVATED_EVENT,
        {"employee_uid": str(employee.uid), "user_uid": str(employee.user.uid) if employee.user_id else None},
        aggregate_type="hr.employee",
        aggregate_uid=employee.uid,
    )


@transaction.atomic
def deactivate_employee(instance: Employee, *, user, expected_version=None, left_on: date | None = None, note: str = "") -> Employee:
    employee = lock(Employee, instance, expected_version, related=("user__role",))
    _guard_account(user, employee, message="You cannot deactivate your own employee record.")
    values = {}
    if employee.is_active:
        values["is_active"] = False
    if left_on is not None and left_on != employee.left_on:
        _check_dates(employee.joined_on, left_on)
        values["left_on"] = left_on
    if not values:
        return employee
    before = employee_snapshot(employee)
    employee.versioned_update(user, **values)
    changed_before, changed_after = changes(before, employee_snapshot(employee))
    record("hr.employee_deactivated", obj=employee, actor=user, before=changed_before, after=changed_after, note=note)
    if "is_active" in values:
        _announce_deactivation(employee)
    recompute.for_employees([employee.uid], *recompute.recent_range(), reason="employee_deactivated")
    _bump()
    return employee


@transaction.atomic
def activate_employee(instance: Employee, *, user, expected_version=None, note: str = "") -> Employee:
    """Back to active (``left_on`` is cleared). The login stays as it is — see the module docstring."""
    employee = lock(Employee, instance, expected_version)
    if employee.is_active:
        return employee
    before = employee_snapshot(employee)
    employee.versioned_update(user, is_active=True, left_on=None)
    changed_before, changed_after = changes(before, employee_snapshot(employee))
    record("hr.employee_activated", obj=employee, actor=user, before=changed_before, after=changed_after, note=note)
    recompute.for_employees([employee.uid], *recompute.recent_range(), reason="employee_activated")
    _bump()
    return employee


def dependency_counts(employee: Employee) -> dict[str, int]:
    """History attached to the employee: leave records (hr) plus the counters other packages register."""
    return {"leave_records": LeaveRecord.all_objects.filter(employee=employee).count(), **registries.counters(registries.employee_dependencies, employee)}


def dependencies(employee: Employee) -> dict:
    counts = dependency_counts(employee)
    return {
        "employee": employee,
        "counts": counts,
        "has_login": employee.user_id is not None,
        "user": employee.user,
        "can_delete": not any(counts.values()),
    }


def device_mappings(employee: Employee) -> dict:
    """Per-device enrolment of the employee, from the devices package (empty until it is installed)."""
    if not registries.device_mappings.available:
        return {"employee": employee, "available": False, "mappings": [], "details": {}}
    data = dict(registries.device_mappings(employee) or {})
    return {"employee": employee, "available": True, "mappings": list(data.pop("mappings", [])), "details": data}


def reconcile_devices(*, user, read_devices: bool = True, apply: bool = False, confirm: bool = False) -> dict:
    """``POST hr/employees/reconcile-devices/`` — preview by default; ``apply`` also needs ``confirm``."""
    if apply and not confirm:
        raise DomainError("confirmation_required", "Applying can create and deactivate employees: preview first, then send confirm=true.", errors={"confirm": ["Required with apply."]})
    if not registries.device_reconciler.available:
        raise DomainError("devices_unavailable", "Device reconciliation is not available yet (the devices package is not installed).", status=503)
    return registries.device_reconciler(user=user, read_devices=read_devices, apply=apply)


@transaction.atomic
def delete_employee(instance: Employee, *, user, expected_version=None) -> None:
    employee = lock(Employee, instance, expected_version, related=("user__role",))
    _guard_account(user, employee, message="You cannot delete your own employee record.")
    counts = {name: count for name, count in dependency_counts(employee).items() if count}
    if counts:
        raise Conflict(
            "employee_has_history",
            f"{employee.full_name} has " + ", ".join(f"{count} {name.replace('_', ' ')}" for name, count in counts.items()) + ". Deactivate the employee instead.",
            errors={name: [str(count)] for name, count in counts.items()},
        )
    before = employee_snapshot(employee)
    if employee.user_id is not None:
        _announce_deactivation(employee)  # the login of a removed profile must not keep working
        employee.versioned_update(user, user_id=None)  # `user` is also the actor parameter
    employee.soft_delete(user)
    record("hr.employee_deleted", obj=employee, actor=user, before=before)
    _bump()
