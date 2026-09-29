"""Stored days from raw punches: the recompute (engine v4), its debounce queue (A8) and the nightly finalisation (A7).

* :func:`recompute` — the one writer of ``attendance_day``. Loads the engine inputs, runs
  ``engines.attendance.recompute`` and writes what changed. Days ≥ today in the employee's office zone are never
  written (A7); corrected days are left alone (A12); a day the engine no longer produces for a person (outside their
  employment dates) is soft-deleted unless corrected. The employees are locked (``SELECT … FOR UPDATE``) so two
  recomputes of the same people serialise instead of colliding on the ``(employee, work_date)`` key.
* :func:`request_recompute` / :func:`run_due` — the debounce: events record what to recompute, a Celery task (never a
  terminal's or an agent's request) runs everything due once, merged per employee.
* :func:`finalise_due` — Beat: after 00:30 on each office's wall clock, yesterday (and the day before, for late
  uploads) is recomputed once for that office's people, so every finalised day is stored even if nobody punched.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from functools import partial

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q

from attendance.models import AttendanceDay, RecomputeRequest
from attendance.services import inputs
from attendance.services.common import bump_days, latest_possible_today, now, office_timezone, platform_timezone
from audit.services import record
from engines import attendance as engine
from hr.models import Employee, Office

logger = logging.getLogger("flarize.attendance")

ENGINE_FIELDS = (
    "first_in",
    "last_out",
    "punch_count",
    "working_minutes",
    "break_minutes",
    "late_minutes",
    "early_exit_minutes",
    "overtime_minutes",
    "is_late",
    "is_early_exit",
    "status",
    "worked_on_off_day",
    "leave_conflict",
    "missing_out",
    "source_raw_ids",
    "ignored_raw_ids",
    "processing_version",
)
WRITE_FIELDS = (*ENGINE_FIELDS, "office_id", "shift_id", "first_device_id")
FINALISE_AFTER = time(0, 30)


def setting(name: str, default):
    return getattr(settings, name, default)


def max_range_days() -> int:
    return int(setting("ATTENDANCE_MAX_RECOMPUTE_DAYS", 400))


def debounce_seconds() -> int:
    return int(setting("ATTENDANCE_RECOMPUTE_DEBOUNCE_SECONDS", 60))


# --------------------------------------------------------------------------------------------------------------------
# the recompute
# --------------------------------------------------------------------------------------------------------------------
def _employees(employee_ids, office_ids, date_from: date):
    """Who is recomputed: live employees who are active, or who left on or after ``date_from`` (their last days)."""
    queryset = inputs.employees_queryset().filter(Q(is_active=True) | Q(left_on__gte=date_from))
    if employee_ids is not None:
        queryset = queryset.filter(pk__in=list(employee_ids))
    if office_ids is not None:
        queryset = queryset.filter(office_id__in=list(office_ids))
    # lock the people (never their office/shift rows): recomputes of the same people serialise
    return list(queryset.select_for_update(of=("self",)).order_by("pk"))


def _values(employee, result: engine.DayResult, at: datetime) -> dict:
    data = result.as_dict()
    shift = inputs.effective_shift(employee)
    values = {name: data[name] for name in ENGINE_FIELDS}
    values["status"] = str(result.status)
    values["office_id"] = employee.office_id
    values["shift_id"] = shift.pk if shift is not None else None
    values["first_device_id"] = result.first_device
    values["computed_at"] = at
    return values


def _clamp(date_from: date, date_to: date, at: datetime) -> tuple[date, date, date | None]:
    """No date beyond the latest possible "today" (A7); at most ``ATTENDANCE_MAX_RECOMPUTE_DAYS`` days, the latest kept."""
    date_to = min(date_to, latest_possible_today(at))
    not_before = None
    limit = max_range_days()
    if (date_to - date_from).days + 1 > limit:
        not_before = date_from
        date_from = date_to - timedelta(days=limit - 1)
    return date_from, date_to, not_before


@transaction.atomic
def recompute(*, date_from: date, date_to: date, employee_ids=None, office_ids=None, reason: str, user=None, at: datetime | None = None) -> dict:
    """Recompute and store the days of ``[date_from, date_to]`` for the chosen people (every employee by default)."""
    at = at or now()
    requested = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    date_from, date_to, clipped = _clamp(date_from, date_to, at)
    counts = {"employees": 0, "created": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped_corrected": 0, "skipped_future": 0, "absent_days": 0, "raw_punches_considered": 0, "unmapped_pins": 0}
    summary = {**requested, "computed_from": date_from.isoformat(), "computed_to": date_to.isoformat(), "not_recomputed_before": clipped.isoformat() if clipped else None, "reason": reason, **counts}
    if date_to < date_from:
        return summary
    people = _employees(employee_ids, office_ids, date_from)
    if not people:
        return summary
    ids = [employee.pk for employee in people]
    staff = inputs.engine_employees(people)
    links = inputs.links(ids)
    result = engine.recompute(
        date_from=date_from,
        date_to=date_to,
        employees=staff,
        punches=inputs.raw_punches(links, date_from, date_to),
        links=links,
        now=at,
        holidays=inputs.holidays(date_from, date_to),
        leaves=inputs.leaves(ids, date_from, date_to),
        rules=inputs.rules(),
        corrected=inputs.corrected_days(ids, date_from, date_to),
    )
    by_pk = {employee.pk: employee for employee in people}
    existing = {(day.employee_id, day.work_date): day for day in AttendanceDay.objects.filter(employee_id__in=ids, work_date__gte=date_from, work_date__lte=date_to)}
    to_create, to_update = [], []
    written = set()
    for write in result.writes:
        key = (write.employee, write.result.work_date)
        written.add(key)
        values = _values(by_pk[write.employee], write.result, at)
        day = existing.get(key)
        if day is None:
            to_create.append(AttendanceDay(employee_id=write.employee, work_date=write.result.work_date, **values))
        elif any(getattr(day, name) != values[name] for name in WRITE_FIELDS):
            for name, value in values.items():
                setattr(day, name, value)
            day.version += 1
            day.updated_at = at
            day.updated_by = None
            to_update.append(day)
        else:
            counts["unchanged"] += 1
    skipped = set(result.skipped_corrected) | set(result.skipped_future)
    stale = [day for key, day in existing.items() if key not in written and key not in skipped and not day.is_corrected]
    if to_create:
        AttendanceDay.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        AttendanceDay.objects.bulk_update(to_update, [*WRITE_FIELDS, "computed_at", "version", "updated_at", "updated_by"], batch_size=500)
    if stale:
        AttendanceDay.objects.filter(pk__in=[day.pk for day in stale]).update(deleted_at=at, updated_at=at)
    counts.update(
        employees=len(people),
        created=len(to_create),
        updated=len(to_update),
        removed=len(stale),
        skipped_corrected=len(result.skipped_corrected),
        skipped_future=len(result.skipped_future),
        absent_days=result.absent_days,
        raw_punches_considered=result.raw_punches_considered,
        unmapped_pins=sum(len(pins) for pins in result.unmapped.values()),
    )
    summary.update(counts)
    if to_create or to_update or stale:
        bump_days()
    record(
        "attendance.recomputed",
        object_type="attendance.attendanceday",
        actor=user,
        actor_kind=None if user is not None else "SYSTEM",
        after={key: summary[key] for key in ("reason", "computed_from", "computed_to", "not_recomputed_before", "employees", "created", "updated", "removed", "skipped_corrected")},
    )
    return summary


# --------------------------------------------------------------------------------------------------------------------
# the debounce queue (A8)
# --------------------------------------------------------------------------------------------------------------------
def _enqueue_run() -> None:
    from attendance.tasks import run_due_recomputes

    run_due_recomputes.apply_async(countdown=debounce_seconds() + 1)


def request_recompute(*, date_from: date, date_to: date, reason: str, employee_ids=None, office_id=None, all_employees: bool = False, delay_seconds: int | None = None) -> int:
    """Queue a recompute (inside the caller's transaction); returns how many requests were recorded.

    Exactly one of ``employee_ids`` (a list of employee pks), ``office_id`` or ``all_employees``.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    due = now() + timedelta(seconds=debounce_seconds() if delay_seconds is None else delay_seconds)
    reason = (reason or "unspecified")[:64]
    common = {"date_from": date_from, "date_to": date_to, "reason": reason, "due_at": due}
    if all_employees:
        rows = [RecomputeRequest(all_employees=True, **common)]
    elif office_id is not None:
        rows = [RecomputeRequest(office_id=office_id, **common)]
    else:
        rows = [RecomputeRequest(employee_id=pk, **common) for pk in sorted(set(employee_ids or ()))]
    if not rows:
        return 0
    RecomputeRequest.objects.bulk_create(rows)
    # one task per debounce window is enough; the Beat safety net runs whatever a lost message left behind
    if cache.add("attendance:recompute:scheduled", 1, timeout=max(1, debounce_seconds())):
        transaction.on_commit(partial(_enqueue_run), robust=True)
    return len(rows)


def _merge(requests: list[RecomputeRequest]) -> list[dict]:
    """Group due requests into as few recompute calls as possible."""
    everyone: tuple[date, date] | None = None
    offices: dict[int, tuple[date, date]] = {}
    people: dict[int, tuple[date, date]] = {}

    def widen(current, row):
        return (row.date_from, row.date_to) if current is None else (min(current[0], row.date_from), max(current[1], row.date_to))

    for row in requests:
        if row.all_employees:
            everyone = widen(everyone, row)
        elif row.office_id is not None:
            offices[row.office_id] = widen(offices.get(row.office_id), row)
        else:
            people[row.employee_id] = widen(people.get(row.employee_id), row)
    calls = []
    if everyone is not None:
        calls.append({"date_from": everyone[0], "date_to": everyone[1]})
    for office_id, (low, high) in sorted(offices.items()):
        calls.append({"date_from": low, "date_to": high, "office_ids": [office_id]})
    grouped: dict[tuple[date, date], list[int]] = defaultdict(list)
    for employee_id, window in people.items():
        grouped[window].append(employee_id)
    for (low, high), ids in sorted(grouped.items()):
        calls.append({"date_from": low, "date_to": high, "employee_ids": sorted(ids)})
    return calls


def run_due(*, at: datetime | None = None, limit: int = 1000) -> dict:
    """Run every due request (merged); a failing batch stays queued with its error and is retried by the next run."""
    at = at or now()
    with transaction.atomic():
        due = list(RecomputeRequest.objects.select_for_update(skip_locked=True).filter(due_at__lte=at).order_by("due_at", "id")[:limit])
        if not due:
            return {"requests": 0, "calls": 0, "failed": 0}
        reasons = sorted({row.reason for row in due})
        calls = _merge(due)
        failed = 0
        try:
            with transaction.atomic():
                for call in calls:
                    recompute(reason=",".join(reasons)[:64], at=at, **call)
                RecomputeRequest.objects.filter(pk__in=[row.pk for row in due]).delete()
        except Exception as exc:  # noqa: BLE001 - the queue keeps the work; the error is recorded and retried
            logger.exception("attendance recompute failed", extra={"requests": len(due)})
            failed = len(due)
            RecomputeRequest.objects.filter(pk__in=[row.pk for row in due]).update(attempts=F("attempts") + 1, last_error=f"{exc.__class__.__name__}: {exc}"[:2000], due_at=at + timedelta(minutes=5))
    return {"requests": len(due), "calls": len(calls), "failed": failed}


def pending() -> dict:
    queryset = RecomputeRequest.objects.all()
    oldest = queryset.order_by("created_at").values_list("created_at", flat=True).first()
    return {"pending": queryset.count(), "failing": queryset.filter(attempts__gt=0).count(), "oldest_created_at": oldest}


# --------------------------------------------------------------------------------------------------------------------
# finalise_day (A7)
# --------------------------------------------------------------------------------------------------------------------
def finalise_target(at: datetime, timezone_name: str) -> date:
    """The last work date that is final on this office's clock: yesterday once it is past 00:30, else the day before."""
    local = engine.local_wall_clock(at, timezone_name)
    target = engine.finalise_target(at, timezone_name)
    return target if local.time() >= FINALISE_AFTER else target - timedelta(days=1)


def finalise_groups() -> list[tuple[int | None, str]]:
    """``(office pk or None, zone)`` for every live office plus the people without an office (platform zone)."""
    groups = [(office.pk, office_timezone(office)) for office in Office.objects.order_by("pk")]
    if Employee.objects.filter(office__isnull=True).exists():
        groups.append((None, platform_timezone()))
    return groups


def finalise_due(*, at: datetime | None = None) -> list[dict]:
    """Beat: recompute each office's newly final days once (a cache marker per office and date; repeating is harmless)."""
    at = at or now()
    results = []
    for office_id, zone_name in finalise_groups():
        target = finalise_target(at, zone_name)
        marker = f"attendance:finalised:{office_id or 'none'}:{target.isoformat()}"
        if not cache.add(marker, 1, timeout=3 * 24 * 3600):
            continue
        employee_ids = list(Employee.objects.filter(office_id=office_id).values_list("pk", flat=True)) if office_id is None else None
        try:
            summary = recompute(
                date_from=target - timedelta(days=1),
                date_to=target,
                office_ids=[office_id] if office_id is not None else None,
                employee_ids=employee_ids,
                reason="finalise_day",
                at=at,
            )
        except Exception:  # noqa: BLE001 - one office's failure must not stop the others; retried on the next tick
            cache.delete(marker)
            logger.exception("attendance finalise failed", extra={"office_id": office_id})
            continue
        results.append({"office_id": office_id, "work_date": target.isoformat(), **{key: summary[key] for key in ("created", "updated", "removed", "skipped_future")}})
    return results
