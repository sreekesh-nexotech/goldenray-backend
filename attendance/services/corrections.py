"""Corrections of processed days (A12; ``attendance.edit``, never on your own day — ``deny_self_action``).

A correction changes one field of one stored day, keeps the value it replaced (``old``) and the reason, and pins the
day (``is_corrected``) so the recompute leaves it alone. One active correction per field of a day (409
``correction_exists``: revoke it first). Revoking restores the field from ``old`` and, when no active correction is left
on the day, un-pins it and queues a recompute of that day (the punches may have changed since). The values are
validated by the engine (``engines.attendance.apply_correction``): a known status, whole non-negative minutes,
booleans, and wall-clock ``first_in``/``last_out`` with OUT not before IN.
"""

from __future__ import annotations

from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from accounts.services.authz import deny_self_action
from attendance.models import AttendanceCorrection, AttendanceDay
from attendance.services.common import bump_days, now
from attendance.services.recompute import request_recompute
from audit.services import record, snapshot
from core.errors import Conflict, DomainError, NotFound
from core.services import check_version, stamp_create
from engines import attendance as engine
from hr.models import Employee

DAY_SNAPSHOT = ("status", "first_in", "last_out", "working_minutes", "break_minutes", "late_minutes", "early_exit_minutes", "overtime_minutes", "is_late", "is_early_exit", "is_corrected")
CLOCK_FIELDS = ("first_in", "last_out")
MINUTE_FIELDS = ("working_minutes", "break_minutes", "late_minutes", "early_exit_minutes", "overtime_minutes")
# a work date's punches span less than two days (overnight shift + buffer), so no minute figure can exceed that
MAX_MINUTES = 2 * 24 * 60


def _invalid(field: str, message: str) -> DomainError:
    return DomainError("validation_error", message, errors={field: [message]})


def _parse(field: str, value):
    """The JSON value of a correction as the engine expects it."""
    if field in CLOCK_FIELDS:
        if value is None:
            return None
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
        except ValueError:  # well-formed but impossible (month 13, hour 25)
            parsed = None
        if parsed is None or parsed.tzinfo is not None:
            raise _invalid("new", f"{field} is an office wall-clock time 'YYYY-MM-DDTHH:MM[:SS]' without a zone, or null.")
        return parsed.replace(microsecond=0)
    if field in MINUTE_FIELDS and isinstance(value, int) and not isinstance(value, bool) and value > MAX_MINUTES:
        raise _invalid("new", f"{field} is at most {MAX_MINUTES} minutes.")
    return value


def _json(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _result(day: AttendanceDay) -> engine.DayResult:
    return engine.DayResult(
        work_date=day.work_date,
        status=day.status,
        first_in=day.first_in,
        last_out=day.last_out,
        punch_count=day.punch_count,
        working_minutes=day.working_minutes,
        break_minutes=day.break_minutes,
        late_minutes=day.late_minutes,
        early_exit_minutes=day.early_exit_minutes,
        overtime_minutes=day.overtime_minutes,
        is_late=day.is_late,
        is_early_exit=day.is_early_exit,
    )


def _lock_day(day: AttendanceDay, expected_version=None) -> AttendanceDay:
    # the employee first — the recompute holds the same lock — so a correction and a recompute never interleave
    list(Employee.objects.select_for_update().filter(pk=day.employee_id).values_list("pk", flat=True))
    locked = AttendanceDay.objects.select_for_update(of=("self",)).select_related("employee").filter(pk=day.pk).first()
    if locked is None:
        raise NotFound("not_found", "Attendance day not found.")
    check_version(locked, expected_version)
    return locked


def _write(day: AttendanceDay, field: str, value, *, user, pinned: bool) -> None:
    setattr(day, field, value)
    day.missing_out = engine.is_missing_out(day.first_in, day.last_out)
    day.is_corrected = pinned
    day.versioned_update(user, **{field: value, "missing_out": day.missing_out, "is_corrected": pinned})


@transaction.atomic
def create_correction(*, user, day: AttendanceDay, field: str, new, reason: str, expected_version=None) -> AttendanceCorrection:
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("reason", "A reason is required.")
    if field not in engine.CORRECTABLE_FIELDS:
        raise _invalid("field", f"Use one of {', '.join(sorted(engine.CORRECTABLE_FIELDS))}.")
    day = _lock_day(day, expected_version)
    deny_self_action(user, day, module="attendance", action="edit", message="You cannot correct your own attendance.")
    value = _parse(field, new)
    try:
        engine.apply_correction(_result(day), field, value)
    except ValueError as exc:
        raise _invalid("new", str(exc)) from None
    if field == "status":
        value = str(engine.Status(value))
    before = snapshot(day, DAY_SNAPSHOT)
    correction = AttendanceCorrection(day=day, field=field, old=_json(getattr(day, field)), new=_json(value), reason=reason)
    stamp_create(correction, user)
    try:
        with transaction.atomic():
            correction.save()
    except IntegrityError:
        raise Conflict("correction_exists", f"{field} of this day is already corrected; revoke that correction first.", errors={"field": ["Already corrected."]}) from None
    _write(day, field, value, user=user, pinned=True)
    record("attendance.day_corrected", obj=day, actor=user, before=before, after={**snapshot(day, DAY_SNAPSHOT), "field": field, "reason": reason, "correction": str(correction.uid)})
    bump_days()
    return correction


@transaction.atomic
def revoke_correction(*, user, correction: AttendanceCorrection, reason: str, expected_version=None) -> AttendanceCorrection:
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("reason", "A reason is required.")
    correction = AttendanceCorrection.objects.select_for_update(of=("self",)).filter(pk=correction.pk).first()
    if correction is None:
        raise NotFound("not_found", "Correction not found.")
    check_version(correction, expected_version)
    if correction.revoked_at is not None:
        raise Conflict("correction_revoked", "This correction was already revoked.")
    day = _lock_day(correction.day)
    deny_self_action(user, day, module="attendance", action="edit", message="You cannot change corrections of your own attendance.")
    before = snapshot(day, DAY_SNAPSHOT)
    at = now()
    correction.versioned_update(user, revoked_at=at, revoked_by=user if getattr(user, "pk", None) else None, revoke_reason=reason)
    old = _parse(correction.field, correction.old)
    try:
        engine.apply_correction(_result(day), correction.field, old)
    except ValueError as exc:
        raise Conflict("correction_revoke_conflict", f"Restoring {correction.field} would contradict another correction of this day: {exc} Revoke that one first.") from None
    still_pinned = AttendanceCorrection.objects.filter(day=day, revoked_at__isnull=True).exclude(pk=correction.pk).exists()
    _write(day, correction.field, old, user=user, pinned=still_pinned)
    record("attendance.correction_revoked", obj=correction, actor=user, before=before, after={**snapshot(day, DAY_SNAPSHOT), "reason": reason})
    if not still_pinned:
        request_recompute(employee_ids=[day.employee_id], date_from=day.work_date, date_to=day.work_date, reason="correction_revoked", delay_seconds=0)
    bump_days()
    return correction
