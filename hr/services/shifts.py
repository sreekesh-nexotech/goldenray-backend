"""``hr/shifts/`` — working-time rules (module ``hr_setup``: view / edit).

* codes are unique among live shifts, case-insensitive (409 ``shift_code_taken``);
* ``is_overnight`` must agree with the clock (an overnight shift ends at or before its start time, a day shift after
  it) and ``half_day_minutes`` may not exceed ``full_day_minutes`` (400, also DB checks);
* an edit of any engine field re-computes the recent attendance of everyone whose effective shift it is
  (``hr.attendance_inputs_changed``);
* a shift still assigned to a live employee or used as an office default cannot be deleted (409 ``shift_in_use``);
  deleting one soft-deletes its attendance rules (true children).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from audit.services import changes, record, snapshot
from core.errors import Conflict
from core.services import stamp_create
from hr.models import AttendanceRule, Office, Shift
from hr.models.shift import MINUTE_FIELDS
from hr.services import recompute, validation
from hr.services.common import bump_setup, lock, unique_conflict

ENGINE_FIELDS = (
    "start_time",
    "end_time",
    "is_overnight",
    *[name for name, _, _ in MINUTE_FIELDS],
    "auto_deduct_break",
    "overtime_enabled",
    "working_days",
    "weekly_off_days",
)
EDITABLE_FIELDS = ("code", "name", *ENGINE_FIELDS, "is_active")
SNAPSHOT_FIELDS = EDITABLE_FIELDS
UNIQUE = {"hr_shift_code_live_uniq": ("shift_code_taken", "code", "Another shift already uses this code.")}


def shifts_queryset():
    live_active = Q(employees__deleted_at__isnull=True, employees__is_active=True)
    return Shift.objects.annotate(employee_count=Count("employees", filter=live_active, distinct=True)).order_by("name", "id")


def shift_snapshot(shift: Shift) -> dict:
    return snapshot(shift, SNAPSHOT_FIELDS)


def normalise(values: dict) -> dict:
    """Validate and normalise the writable fields present in ``values`` (in place)."""
    if "code" in values:
        values["code"] = validation.code(values["code"], max_length=30)
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise validation.invalid("name", "This field may not be blank.")
    for field in ("working_days", "weekly_off_days"):
        if field in values:
            values[field] = validation.weekdays(values[field], field)
    for name, _, maximum in MINUTE_FIELDS:
        if name in values and not 0 <= int(values[name]) <= maximum:
            raise validation.invalid(name, f"Must be between 0 and {maximum} minutes.")
    return values


def check_state(shift: Shift) -> None:
    """Cross-field rules of the resulting row (the DB checks the same, this answers 400 instead of 500)."""
    if shift.is_overnight and shift.end_time > shift.start_time:
        raise validation.invalid("end_time", "An overnight shift ends on the next day: end_time must be at or before start_time.")
    if not shift.is_overnight and shift.end_time <= shift.start_time:
        raise validation.invalid("end_time", "end_time must be after start_time (mark the shift overnight if it ends on the next day).")
    if shift.half_day_minutes > shift.full_day_minutes:
        raise validation.invalid("half_day_minutes", "A half day cannot be longer than a full day.")


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


@transaction.atomic
def create_shift(*, user, data) -> Shift:
    values = normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    shift = Shift(**values)
    check_state(shift)
    stamp_create(shift, user)
    _save(shift.save)
    record("hr.shift_created", obj=shift, actor=user, after=shift_snapshot(shift))
    bump_setup()
    return shift


@transaction.atomic
def update_shift(instance: Shift, *, user, data, expected_version=None) -> Shift:
    shift = lock(Shift, instance, expected_version)
    before = shift_snapshot(shift)
    values = normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(shift, name) != value}
    if not values:
        return shift
    probe = Shift(**{**{name: getattr(shift, name) for name in EDITABLE_FIELDS}, **values})
    check_state(probe)
    _save(lambda: shift.versioned_update(user, **values))
    changed_before, changed_after = changes(before, shift_snapshot(shift))
    record("hr.shift_updated", obj=shift, actor=user, before=changed_before, after=changed_after)
    if set(values) & set(ENGINE_FIELDS):
        recompute.for_employees(recompute.employees_on_shift(shift), *recompute.recent_range(), reason="shift_updated")
    bump_setup()
    return shift


@transaction.atomic
def delete_shift(instance: Shift, *, user, expected_version=None) -> None:
    shift = lock(Shift, instance, expected_version)
    employees = shift.employees.filter(deleted_at__isnull=True).count()
    offices = Office.objects.filter(default_shift=shift).count()
    if employees or offices:
        raise Conflict(
            "shift_in_use",
            f"'{shift.name}' is assigned to {employees} employee(s) and is the default of {offices} office(s). Deactivate it or reassign them first.",
            errors={"employees": [str(employees)], "offices": [str(offices)]},
        )
    for rule in AttendanceRule.objects.filter(shift=shift):
        rule.soft_delete(user)
        record("hr.attendance_rule_deleted", obj=rule, actor=user, note=f"shift {shift.code} deleted")
    shift.soft_delete(user)
    record("hr.shift_deleted", obj=shift, actor=user, before=shift_snapshot(shift))
    bump_setup()
