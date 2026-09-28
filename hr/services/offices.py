"""``hr/offices/`` — offices (module ``hr_setup``: view / edit) and the office summary.

* codes are unique among live offices, case-insensitive (409 ``office_code_taken``); ``timezone`` must be an IANA
  zone known to zoneinfo (400) — eSSL stored anything;
* the default shift is the office's "attendance settings" (eSSL ``attendance_settings``); changing it, or the time
  zone, re-computes the recent attendance of the office's people;
* an office with live employees, or with dependants registered by other packages (devices, agents), cannot be
  deleted (409 ``office_in_use``); deleting one soft-deletes its holidays and attendance rules (true children);
* ``summary(office, day, user)`` = hr's own numbers (people, leave, holiday, weekly off) plus the sections other
  packages register in :data:`hr.registries.office_summary`.
"""

from __future__ import annotations

from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from audit.services import changes, record, snapshot
from core.errors import Conflict
from core.services import stamp_create
from hr import registries
from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, Office
from hr.services import recompute, validation
from hr.services.common import bump_setup, lock, today_in, unique_conflict, validate_timezone

EDITABLE_FIELDS = ("code", "name", "address", "timezone", "default_shift", "is_active")
SNAPSHOT_FIELDS = EDITABLE_FIELDS
UNIQUE = {"hr_office_code_live_uniq": ("office_code_taken", "code", "Another office already uses this code.")}


def offices_queryset():
    live_active = Q(employees__deleted_at__isnull=True, employees__is_active=True)
    return Office.objects.select_related("default_shift").annotate(employee_count=Count("employees", filter=live_active, distinct=True)).order_by("name", "id")


def office_snapshot(office: Office) -> dict:
    return snapshot(office, SNAPSHOT_FIELDS)


def _normalise(values: dict) -> dict:
    if "code" in values:
        values["code"] = validation.code(values["code"], max_length=30)
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise validation.invalid("name", "This field may not be blank.")
    if "timezone" in values:
        values["timezone"] = validate_timezone(values["timezone"])
    if "address" in values:
        values["address"] = values["address"] or ""
    return values


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


@transaction.atomic
def create_office(*, user, data) -> Office:
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    office = Office(**values)
    stamp_create(office, user)
    _save(office.save)
    record("hr.office_created", obj=office, actor=user, after=office_snapshot(office))
    bump_setup()
    return office


@transaction.atomic
def update_office(instance: Office, *, user, data, expected_version=None) -> Office:
    office = lock(Office, instance, expected_version)
    before = office_snapshot(office)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(office, name) != value}
    if not values:
        return office
    _save(lambda: office.versioned_update(user, **values))
    changed_before, changed_after = changes(before, office_snapshot(office))
    record("hr.office_updated", obj=office, actor=user, before=changed_before, after=changed_after)
    if "timezone" in values:
        recompute.for_employees(recompute.employees_in_office(office), *recompute.recent_range(), reason="office_timezone_changed")
    elif "default_shift" in values:
        affected = Employee.objects.filter(office=office, shift__isnull=True).values_list("uid", flat=True)
        recompute.for_employees(affected, *recompute.recent_range(), reason="office_default_shift_changed")
    bump_setup()
    return office


@transaction.atomic
def delete_office(instance: Office, *, user, expected_version=None) -> None:
    office = lock(Office, instance, expected_version)
    counts = {"employees": Employee.objects.filter(office=office).count(), **registries.counters(registries.office_dependencies, office)}
    blocking = {name: count for name, count in counts.items() if count}
    if blocking:
        raise Conflict(
            "office_in_use",
            f"'{office.name}' still has " + ", ".join(f"{count} {name}" for name, count in blocking.items()) + ". Deactivate the office or move them first.",
            errors={name: [str(count)] for name, count in blocking.items()},
        )
    for child in [*Holiday.objects.filter(office=office), *AttendanceRule.objects.filter(office=office)]:
        child.soft_delete(user)
        record(f"hr.{'holiday' if isinstance(child, Holiday) else 'attendance_rule'}_deleted", obj=child, actor=user, note=f"office {office.code} deleted")
    office.soft_delete(user)
    record("hr.office_deleted", obj=office, actor=user, before=office_snapshot(office))
    bump_setup()


def holiday_on(office: Office, day: date) -> Holiday | None:
    """The active holiday of ``office`` on ``day``; an office holiday wins over a global one."""
    rows = Holiday.objects.filter(Q(office=office) | Q(office__isnull=True), date=day, is_active=True).order_by("office_id")
    return next((row for row in rows if row.office_id is not None), None) or next(iter(rows), None)


def summary(office: Office, day: date | None, user) -> dict:
    """Headline numbers for one office on one day (default: today in the office's zone)."""
    day = day or today_in(office.timezone)
    employees = Employee.objects.filter(office=office).aggregate(total=Count("id"), active=Count("id", filter=Q(is_active=True)))
    leave = LeaveRecord.objects.filter(employee__office=office, employee__deleted_at__isnull=True, date_from__lte=day, date_to__gte=day)
    leave_counts = leave.aggregate(on_leave=Count("employee", filter=Q(status=LeaveRecord.Status.APPROVED), distinct=True), pending=Count("id", filter=Q(status=LeaveRecord.Status.PENDING)))
    holiday = holiday_on(office, day)
    shift = office.default_shift
    weekly_off = bool(shift and day.weekday() in (shift.weekly_off_days or []))
    working_day = bool(shift is None or (day.weekday() in (shift.working_days or []) and not weekly_off)) and holiday is None
    return {
        "office": office,
        "date": day,
        "employees": {"total": employees["total"], "active": employees["active"]},
        "leave": {"on_leave": leave_counts["on_leave"], "pending": leave_counts["pending"]},
        "holiday": {"uid": holiday.uid, "name": holiday.name, "is_global": holiday.office_id is None} if holiday else None,
        "is_weekly_off": weekly_off,
        "is_working_day": working_day,
        "sections": registries.office_summary.collect(office, day, user),
    }
