"""Attendance engine v4: the eSSL day computation as pure functions over plain dataclasses.

Port of the eSSL ``services/processing.py`` (``PROCESSING_VERSION = "v3-half-day-arrival"``) and
``services/calendar_service.py`` with the behaviour changes A1–A12 of PLAN §2.9:

* A1  identity is ``(device, pin)`` — :func:`build_identity_map`, :func:`unmapped_pins`; no global PIN or
      ``employee_code`` fallback.
* A2  ``break_deducted = min(break_minutes, max(0, gross − half_day_minutes))`` — :func:`deduct_break`.
* A3  punches within ``debounce_minutes`` of the previous accepted punch are ignored (kept in ``ignored_raw_ids``) —
      :func:`debounce`.
* A4  a worked holiday/weekly off keeps its status with ``worked_on_off_day`` and overtime = the working minutes;
      a full-day leave with punches is computed from the punches with ``leave_conflict``.
* A5  half-day deadline = ``shift.start + half_day_after_minutes``, overridable by attendance rules —
      :func:`half_day_deadline`, :func:`rules_for`.
* A6  overnight shifts attribute punches up to ``end_time + overnight_buffer_minutes`` to the previous work date —
      :func:`attribute_work_date`.
* A7  nothing is produced for dates ≥ today in the office timezone (:func:`recompute`); calendars leave those
      days blank (:func:`calendar_fill`); :func:`finalise_target` names the day ``finalise_day`` writes.
* A8  :func:`affected_work_dates` gives the work dates an ingestion touches (the recompute is event-driven in the
      attendance app).
* A9  one :func:`calendar_fill`, one :func:`attendance_rate` ``(present + 0.5 × half) / expected`` with leave out
      of the denominator, unambiguous codes (:data:`STATUS_CODE`).
* A10 punches are timezone-aware instants read on the office's wall clock; :func:`punch_at_from_device_time`,
      :func:`today_local`, :func:`clock_offset_seconds` (measured, never applied).
* A11 :func:`punch_dedup_key` — ``sha256(serial|pin|device_time|status|punch)`` for every transport.
* A12 corrected days are skipped by :func:`recompute`; :func:`apply_correction` validates a correction.

Conventions: every instant handed in is timezone-aware; ``first_in``/``last_out`` and deadlines come back as naive
office-local wall-clock datetimes (``attendance_day`` stores ``timestamp``); minutes are whole, floored, never
negative; nothing here reads a clock — "today" is derived from an explicit ``now``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Callable, Hashable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROCESSING_VERSION = "v4"
DEFAULT_TIMEZONE = "Asia/Kolkata"

#: With no shift at all there is no timing: Sunday is the only day off and 480 minutes is a full day (v3).
DEFAULT_FULL_DAY_MINUTES = 480
#: Double-scan protection still applies to an employee without a shift (A3); the shift field default.
DEFAULT_DEBOUNCE_MINUTES = 2
#: How a day with an IN and no OUT is worded everywhere (v3 ``MISSING_OUT_LABEL``).
MISSING_OUT_LABEL = "Missing OUT"


class Status(StrEnum):
    PRESENT = "PRESENT"
    LATE = "LATE"
    ABSENT = "ABSENT"
    HALF_DAY = "HALF_DAY"
    WEEKLY_OFF = "WEEKLY_OFF"
    HOLIDAY = "HOLIDAY"
    ON_LEAVE = "ON_LEAVE"


#: A9: one letter set, no letter with two meanings (v3 used ``H`` for holiday and, in weekly reports, half day;
#: ``L`` for late and ``Leave``/``O`` for leave).
STATUS_CODE = {
    Status.PRESENT: "P",
    Status.LATE: "LT",
    Status.ABSENT: "A",
    Status.HALF_DAY: "HD",
    Status.WEEKLY_OFF: "WO",
    Status.HOLIDAY: "H",
    Status.ON_LEAVE: "L",
}
STATUS_LABEL = {
    Status.PRESENT: "Present",
    Status.LATE: "Late",
    Status.ABSENT: "Absent",
    Status.HALF_DAY: "Half Day",
    Status.WEEKLY_OFF: "Weekly Off",
    Status.HOLIDAY: "Holiday",
    Status.ON_LEAVE: "Leave",
}

#: Days attended: a half day is a late arrival, not an absence (v3 ``PRESENT_DAY_STATUSES``).
PRESENT_DAY_STATUSES = frozenset({Status.PRESENT, Status.LATE, Status.HALF_DAY})


def counts_as_present(status: str | None) -> bool:
    return status in PRESENT_DAY_STATUSES


def is_missing_out(first_in: datetime | None, last_out: datetime | None) -> bool:
    """Somebody arrived and no closing punch was recorded. Reported, never filled in."""
    return first_in is not None and last_out is None


# ---------------------------------------------------------------------------------------------------------------
# time zones and clocks (A7, A10, A11)
# ---------------------------------------------------------------------------------------------------------------


def zone(name: str) -> ZoneInfo:
    """The office timezone; an unknown name is an error (v3 silently fell back to Asia/Kolkata, then UTC)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("A timezone name is required.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {name!r}.") from exc


def is_valid_timezone(name: Any) -> bool:
    try:
        zone(name)
    except ValueError:
        return False
    return True


def _require_aware(value: Any, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime.")
    return value


def _require_naive(value: Any, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError(f"{what} must be a naive datetime (the wall clock as reported).")
    return value


def today_local(now: datetime, timezone: str) -> date:
    """ "Today" on the office's wall clock (A10: every "today" uses the office timezone)."""
    return _require_aware(now, "now").astimezone(zone(timezone)).date()


def local_wall_clock(instant: datetime, timezone: str) -> datetime:
    """An instant read on the office's wall clock, as a naive datetime."""
    return _require_aware(instant, "instant").astimezone(zone(timezone)).replace(tzinfo=None)


def punch_at_from_device_time(device_time: datetime, timezone: str) -> datetime:
    """``punch_at`` for a terminal's naive wall-clock ``device_time`` in its office timezone (A10).

    The device's clock offset is *not* applied: it is measured and displayed, never corrected.
    """
    return _require_naive(device_time, "device_time").replace(tzinfo=zone(timezone))


def clock_offset_seconds(device_clock: datetime, reference: datetime, timezone: str) -> int:
    """How far the terminal's clock is ahead (+) or behind (−) the server, in whole seconds (A10)."""
    device_instant = punch_at_from_device_time(device_clock, timezone)
    return round((device_instant - _require_aware(reference, "reference")).total_seconds())


def finalise_target(now: datetime, timezone: str) -> date:
    """The work date the nightly ``finalise_day`` task writes: yesterday on the office's wall clock (A7)."""
    return today_local(now, timezone) - timedelta(days=1)


def punch_dedup_key(serial: str, pin: Any, device_time: datetime, status: int | None = None, punch: int | None = None) -> str:
    """A11: ``sha256(serial|pin|device_time|status|punch)`` — the same punch delivered by the agent and by ADMS
    collapses to one row; the device's record uid is data, not identity."""
    _require_naive(device_time, "device_time")
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("serial is required.")
    pin_text = str(pin).strip()
    if not pin_text:
        raise ValueError("pin is required.")
    parts = (serial.strip().upper(), pin_text, device_time.isoformat(), "" if status is None else str(int(status)), "" if punch is None else str(int(punch)))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# plain data
# ---------------------------------------------------------------------------------------------------------------

_SHIFT_MINUTE_FIELDS = (
    "overnight_buffer_minutes",
    "grace_minutes",
    "late_threshold_minutes",
    "early_exit_threshold_minutes",
    "full_day_minutes",
    "half_day_minutes",
    "half_day_after_minutes",
    "break_minutes",
    "debounce_minutes",
    "overtime_after_minutes",
)


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Shift:
    """``hr_shift`` (PLAN §2.9) with its defaults. ``key`` identifies the shift for attendance-rule matching."""

    start_time: time
    end_time: time
    key: Hashable | None = None
    is_overnight: bool = False
    overnight_buffer_minutes: int = 180
    grace_minutes: int = 10
    late_threshold_minutes: int = 0
    early_exit_threshold_minutes: int = 15
    full_day_minutes: int = 480
    half_day_minutes: int = 240
    half_day_after_minutes: int = 30
    break_minutes: int = 60
    auto_deduct_break: bool = True
    debounce_minutes: int = 2
    overtime_enabled: bool = True
    overtime_after_minutes: int = 480
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5)
    weekly_off_days: tuple[int, ...] = (6,)

    def __post_init__(self):
        for name in ("start_time", "end_time"):
            value = getattr(self, name)
            if not isinstance(value, time) or value.tzinfo is not None:
                raise ValueError(f"Shift.{name} must be a naive time.")
        for name in _SHIFT_MINUTE_FIELDS:
            value = getattr(self, name)
            if not _is_whole(value) or value < 0:
                raise ValueError(f"Shift.{name} must be a whole number of minutes >= 0.")
        for name in ("working_days", "weekly_off_days"):
            days = tuple(getattr(self, name) or ())
            if any(not _is_whole(day) or not 0 <= day <= 6 for day in days):
                raise ValueError(f"Shift.{name} holds weekdays 0 (Monday) … 6 (Sunday).")
            object.__setattr__(self, name, days)


@dataclass(frozen=True)
class Punch:
    """One raw punch of an employee: ``punch_at`` is the aware instant (``attendance_raw_punch.punch_at``)."""

    raw_id: Hashable
    punch_at: datetime
    device: Hashable | None = None

    def __post_init__(self):
        _require_aware(self.punch_at, "Punch.punch_at")


@dataclass(frozen=True)
class DayContext:
    """The non-working reasons for one employee-day (holiday on the posting office or all offices, approved leave)."""

    is_holiday: bool = False
    holiday_name: str | None = None
    on_leave: bool = False
    leave_is_half_day: bool = False
    leave_type: str | None = None


@dataclass(frozen=True)
class DayRules:
    """The half-day knobs resolved for one employee-day (``hr_attendance_rule.rules``).

    ``half_day_after`` is a wall-clock deadline, ``half_day_after_minutes`` minutes past the shift start (the shift's
    own ``half_day_after_minutes`` is the default), ``half_day_under_minutes`` restores a worked-minutes condition
    that still needs a late arrival.
    """

    half_day_after: time | None = None
    half_day_after_minutes: int | None = None
    half_day_under_minutes: int | None = None


@dataclass(frozen=True)
class AttendanceRule:
    """``hr_attendance_rule``: global (no office, no shift), office, or shift scope."""

    rules: Mapping[str, Any]
    office: Hashable | None = None
    shift: Hashable | None = None
    effective_from: date | None = None
    is_active: bool = True
    key: Hashable | None = None

    @property
    def scope_rank(self) -> int:
        return 2 if self.shift is not None else (1 if self.office is not None else 0)


@dataclass(frozen=True)
class DayResult:
    """One processed employee-day (the ``attendance_day`` columns the engine owns)."""

    work_date: date
    status: str
    first_in: datetime | None = None
    last_out: datetime | None = None
    first_device: Hashable | None = None
    punch_count: int = 0
    working_minutes: int = 0
    break_minutes: int = 0
    late_minutes: int = 0
    early_exit_minutes: int = 0
    overtime_minutes: int = 0
    is_late: bool = False
    is_early_exit: bool = False
    worked_on_off_day: bool = False
    leave_conflict: bool = False
    source_raw_ids: tuple = ()
    ignored_raw_ids: tuple = ()
    processing_version: str = PROCESSING_VERSION

    @property
    def missing_out(self) -> bool:
        return is_missing_out(self.first_in, self.last_out)

    def as_dict(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["status"] = str(self.status)
        data["source_raw_ids"] = list(self.source_raw_ids)
        data["ignored_raw_ids"] = list(self.ignored_raw_ids)
        data["missing_out"] = self.missing_out
        return data


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def _instant(punch: Punch) -> datetime:
    return punch.punch_at.astimezone(UTC)


# ---------------------------------------------------------------------------------------------------------------
# shift, work date, working day
# ---------------------------------------------------------------------------------------------------------------


def effective_shift(employee_shift: Shift | None, office_default_shift: Shift | None = None) -> Shift | None:
    """The employee's own shift, else the office default, else none (no timing rules at all)."""
    return employee_shift if employee_shift is not None else office_default_shift


def _seconds(value: time) -> float:
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000


def overnight_cutoff(shift: Shift) -> float:
    """Seconds after midnight up to which (inclusive) a punch belongs to the previous work date (A6).

    ``end_time + overnight_buffer_minutes``, but never reaching the shift's own start: a punch at the start opens the
    new work day.
    """
    cutoff = _seconds(shift.end_time) + shift.overnight_buffer_minutes * 60
    start = _seconds(shift.start_time)
    if cutoff >= start:
        cutoff = start - 1
    return min(cutoff, 86_399.999_999)


def attribute_work_date(wall_clock: datetime, shift: Shift | None) -> date:
    """The work date of a punch read on the office's wall clock (A6: overnight end buffer)."""
    if shift is None or not shift.is_overnight:
        return wall_clock.date()
    moment = wall_clock.hour * 3600 + wall_clock.minute * 60 + wall_clock.second + wall_clock.microsecond / 1_000_000
    if moment <= overnight_cutoff(shift):
        return wall_clock.date() - timedelta(days=1)
    return wall_clock.date()


def shift_boundaries(work_date: date, shift: Shift) -> tuple[datetime, datetime]:
    """Shift start and end on the office's wall clock (an end not after the start rolls to the next day)."""
    start = datetime.combine(work_date, shift.start_time)
    end_day = work_date + timedelta(days=1) if shift.is_overnight else work_date
    end = datetime.combine(end_day, shift.end_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def is_working_day(work_date: date, shift: Shift | None) -> bool:
    """Weekly off wins over the working-day list; an empty list means every day; no shift means Sunday off."""
    weekday = work_date.weekday()
    if shift is None:
        return weekday != 6
    if weekday in shift.weekly_off_days:
        return False
    if not shift.working_days:
        return True
    return weekday in shift.working_days


def deduct_break(gross_minutes: int, shift: Shift | None) -> tuple[int, int]:
    """``(working, break_deducted)`` for a measured span (A2: continuous and monotonic).

    ``break_deducted = min(break_minutes, max(0, gross − half_day_minutes))`` — v3 deducted the whole break only once
    the span exceeded it (60 minutes → 60 worked, 61 → 1).
    """
    gross = max(0, int(gross_minutes))
    if gross == 0 or shift is None or not shift.auto_deduct_break:
        return gross, 0
    deducted = min(shift.break_minutes, max(0, gross - shift.half_day_minutes))
    return gross - deducted, deducted


def debounce(punches: Iterable[Punch], minutes: int) -> tuple[tuple[Punch, ...], tuple[Punch, ...]]:
    """``(accepted, ignored)``: a punch within ``minutes`` of the previous *accepted* punch is ignored (A3).

    ``minutes <= 0`` disables the debounce. The first punch is always accepted.
    """
    ordered = sorted(punches, key=_instant)
    if minutes <= 0:
        return tuple(ordered), ()
    window = timedelta(minutes=minutes)
    accepted: list[Punch] = []
    ignored: list[Punch] = []
    for punch in ordered:
        if accepted and _instant(punch) - _instant(accepted[-1]) < window:
            ignored.append(punch)
        else:
            accepted.append(punch)
    return tuple(accepted), tuple(ignored)


# ---------------------------------------------------------------------------------------------------------------
# attendance rules and the half-day deadline (A5)
# ---------------------------------------------------------------------------------------------------------------

RULE_KEYS = ("half_day_after", "half_day_after_minutes", "half_day_under_minutes")
#: A rule's minutes are a deadline allowance or a worked-minutes bar: a day at most. ``rules`` is JSON, so nothing else
#: bounds them — an unbounded allowance pushed the deadline past year 9999 and every compute_day in scope raised.
MAX_RULE_MINUTES = 24 * 60


def _parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(raw, fmt).time()
            except ValueError:
                continue
    return None


def _parse_minutes(value: Any) -> int | None:
    """Whole minutes in ``0 … MAX_RULE_MINUTES``, else ``None`` (malformed)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an infinite float or Decimal
        return None
    if isinstance(value, (float, Decimal)) and minutes != value:  # 30.5 is not a whole number of minutes
        return None
    return minutes if 0 <= minutes <= MAX_RULE_MINUTES else None


def rules_from_payload(payload: Mapping[str, Any] | None) -> DayRules:
    """The recognised keys of one rule's JSON; anything unrecognised or malformed is ignored (v3) — a document that
    is not an object included."""
    data = payload if isinstance(payload, Mapping) else {}
    return DayRules(
        half_day_after=_parse_time(data.get("half_day_after")),
        half_day_after_minutes=_parse_minutes(data.get("half_day_after_minutes")),
        half_day_under_minutes=_parse_minutes(data.get("half_day_under_minutes")),
    )


def validate_rules_payload(payload: Any) -> dict[str, str]:
    """Field errors for an ``hr_attendance_rule.rules`` document (empty when valid)."""
    if not isinstance(payload, Mapping):
        return {"rules": "Must be an object."}
    errors: dict[str, str] = {}
    for key in payload:
        if key not in RULE_KEYS:
            errors[str(key)] = f"Unknown key; allowed: {', '.join(RULE_KEYS)}."
    if payload.get("half_day_after") is not None and _parse_time(payload["half_day_after"]) is None:
        errors["half_day_after"] = "Must be a clock time HH:MM or HH:MM:SS."
    for key in ("half_day_after_minutes", "half_day_under_minutes"):
        if payload.get(key) is not None and (isinstance(payload[key], str) or _parse_minutes(payload[key]) is None):
            errors[key] = f"Must be a whole number of minutes between 0 and {MAX_RULE_MINUTES}."
    return errors


def rules_for(rules: Iterable[AttendanceRule], *, office: Hashable | None, shift: Shift | None, work_date: date) -> DayRules:
    """The rules that apply, most specific last: global < office < shift, then by ``effective_from``.

    A rule dated after the work date or inactive does not apply. Keys a rule does not state are inherited. The deadline
    is one setting stated two ways, so a later rule that states it (as a clock time or as minutes) replaces whatever an
    earlier rule said — v3 let an office clock time beat a more specific shift rule stated in minutes. Within one rule
    the clock time wins.
    """
    shift_key = shift.key if shift is not None else None

    def applies(rule: AttendanceRule) -> bool:
        if not rule.is_active:
            return False
        if rule.effective_from is not None and rule.effective_from > work_date:
            return False
        if rule.shift is not None:
            return shift_key is not None and rule.shift == shift_key
        if rule.office is not None:
            return office is not None and rule.office == office
        return True

    merged: dict[str, Any] = {}
    for rule in sorted((r for r in rules if applies(r)), key=lambda r: (r.scope_rank, r.effective_from or date.min)):
        parsed = rules_from_payload(rule.rules)
        if parsed.half_day_after is not None:
            merged["half_day_after"] = parsed.half_day_after
            merged.pop("half_day_after_minutes", None)
        elif parsed.half_day_after_minutes is not None:
            merged["half_day_after_minutes"] = parsed.half_day_after_minutes
            merged.pop("half_day_after", None)
        if parsed.half_day_under_minutes is not None:
            merged["half_day_under_minutes"] = parsed.half_day_under_minutes
    return DayRules(**merged)


def half_day_deadline(work_date: date, shift: Shift | None, rules: DayRules | None = None) -> datetime | None:
    """The office-local instant after which an arrival makes a short day a half day (A5).

    Default: ``shift start + shift.half_day_after_minutes`` (v3: 10:00 wall clock whatever the start). A rule's
    ``half_day_after_minutes`` replaces the allowance; a rule's ``half_day_after`` states a clock time, which rolls to
    the next day when it is earlier than an overnight start. Without a shift only a rule's clock time gives a deadline.
    """
    rules = rules or DayRules()
    if shift is None:
        return datetime.combine(work_date, rules.half_day_after) if rules.half_day_after is not None else None
    start, _ = shift_boundaries(work_date, shift)
    if rules.half_day_after is not None:
        deadline = datetime.combine(start.date(), rules.half_day_after)
        if deadline < start:
            deadline += timedelta(days=1)
        return deadline
    allowance = rules.half_day_after_minutes if rules.half_day_after_minutes is not None else shift.half_day_after_minutes
    return start + timedelta(minutes=allowance)


def describe_half_day_deadline(shift: Shift, rules: DayRules | None, on: date) -> tuple[time, str]:
    """The deadline a shift runs under on ``on`` and where it comes from (``"rule"`` or ``"shift"``), for display."""
    rules = rules or DayRules()
    stated = rules.half_day_after is not None or rules.half_day_after_minutes is not None
    return half_day_deadline(on, shift, rules).time(), ("rule" if stated else "shift")


# ---------------------------------------------------------------------------------------------------------------
# one day
# ---------------------------------------------------------------------------------------------------------------


def _no_punch_status(ctx: DayContext, working_day: bool) -> Status:
    if ctx.on_leave:
        return Status.ON_LEAVE
    if ctx.is_holiday:
        return Status.HOLIDAY
    if not working_day:
        return Status.WEEKLY_OFF
    return Status.ABSENT


def compute_day(
    work_date: date,
    punches: Sequence[Punch],
    shift: Shift | None,
    context: DayContext | None = None,
    rules: DayRules | None = None,
    *,
    timezone: str = DEFAULT_TIMEZONE,
) -> DayResult:
    """Turn one employee-day's punches (every device, already attributed to ``work_date``) into one processed day.

    First accepted punch = IN, last accepted punch = OUT when there are two or more; device status/punch codes are
    never used. Without punches: leave > holiday > weekly off > absent. With punches (A4): a holiday or weekly off keeps
    its status (``worked_on_off_day``, overtime = working minutes, no lateness); otherwise the status comes from the
    punches — half-day leave → HALF_DAY; a late arrival past the deadline short of a full day → HALF_DAY; else LATE or
    PRESENT — and a full-day leave is flagged ``leave_conflict``.
    """
    tz = zone(timezone)
    ctx = context or DayContext()
    rules = rules or DayRules()
    for punch in punches:
        if not isinstance(punch, Punch):
            raise TypeError("compute_day takes engines.attendance.Punch objects.")
    accepted, ignored = debounce(punches, shift.debounce_minutes if shift is not None else DEFAULT_DEBOUNCE_MINUTES)
    working_day = is_working_day(work_date, shift)
    ignored_ids = tuple(p.raw_id for p in ignored)

    if not accepted:
        return DayResult(work_date=work_date, status=_no_punch_status(ctx, working_day), ignored_raw_ids=ignored_ids)

    first = accepted[0]
    last = accepted[-1] if len(accepted) > 1 else None
    first_in = first.punch_at.astimezone(tz).replace(tzinfo=None)
    last_out = last.punch_at.astimezone(tz).replace(tzinfo=None) if last is not None else None
    gross = _minutes(_instant(last) - _instant(first)) if last is not None else 0
    working, break_taken = deduct_break(gross, shift)
    full_day_leave = ctx.on_leave and not ctx.leave_is_half_day
    common = dict(
        work_date=work_date,
        first_in=first_in,
        last_out=last_out,
        first_device=first.device,
        punch_count=len(accepted),
        working_minutes=working,
        break_minutes=break_taken,
        leave_conflict=full_day_leave,
        source_raw_ids=tuple(p.raw_id for p in accepted),
        ignored_raw_ids=ignored_ids,
    )

    if ctx.is_holiday or not working_day:
        # A4: nobody was scheduled, so there is no lateness, early exit or deadline; every worked minute is overtime.
        overtime = working if (shift is None or shift.overtime_enabled) else 0
        status = Status.HOLIDAY if ctx.is_holiday else Status.WEEKLY_OFF
        return DayResult(status=status, overtime_minutes=overtime, worked_on_off_day=True, **common)

    late_minutes = early_exit_minutes = overtime_minutes = grace_credit = 0
    is_late = is_early_exit = False
    if shift is not None:
        start, end = shift_boundaries(work_date, shift)
        allowed_in = start + timedelta(minutes=shift.grace_minutes)
        if first_in > allowed_in:
            late_minutes = _minutes(first_in - allowed_in)
            is_late = late_minutes > shift.late_threshold_minutes
        elif first_in > start and last_out is not None:
            # Minutes the grace period forgives: they decide the status only, reported hours stay measured.
            grace_credit = _minutes(first_in - start)
        if last_out is not None and last_out < end:
            early = _minutes(end - last_out)
            if early >= shift.early_exit_threshold_minutes:
                early_exit_minutes, is_early_exit = early, True
        if shift.overtime_enabled and working > shift.overtime_after_minutes:
            overtime_minutes = working - shift.overtime_after_minutes

    full_day = shift.full_day_minutes if shift is not None else DEFAULT_FULL_DAY_MINUTES
    credited = working + grace_credit
    deadline = half_day_deadline(work_date, shift, rules)
    arrived_late = deadline is not None and first_in > deadline
    under = rules.half_day_under_minutes
    short_day = under is not None and last_out is not None and credited < under

    if ctx.on_leave and ctx.leave_is_half_day:
        status = Status.HALF_DAY
    elif arrived_late and (under is None or short_day) and credited < full_day:
        status = Status.HALF_DAY
    else:
        status = Status.LATE if is_late else Status.PRESENT

    return DayResult(
        status=status,
        late_minutes=late_minutes,
        early_exit_minutes=early_exit_minutes,
        overtime_minutes=overtime_minutes,
        is_late=is_late,
        is_early_exit=is_early_exit,
        **common,
    )


# ---------------------------------------------------------------------------------------------------------------
# holidays, leave, identity
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Holiday:
    """``hr_holiday``: ``office=None`` applies to every office."""

    date: date
    name: str
    office: Hashable | None = None
    is_active: bool = True


@dataclass(frozen=True)
class Leave:
    """``hr_leave_record``; only APPROVED leave counts."""

    employee: Hashable
    date_from: date
    date_to: date
    status: str = "APPROVED"
    is_half_day: bool = False
    leave_type: str | None = None


class HolidayCalendar:
    """Active holidays keyed by (office, date); an office's own holiday is named before an all-offices one."""

    def __init__(self, holidays: Iterable[Holiday] = ()):
        self._by_key: dict[tuple[Hashable | None, date], str] = {}
        for holiday in holidays:
            if holiday.is_active:
                self._by_key[(holiday.office, holiday.date)] = holiday.name

    def name(self, office: Hashable | None, day: date) -> str | None:
        own = self._by_key.get((office, day)) if office is not None else None
        return own if own is not None else self._by_key.get((None, day))


class LeaveBook:
    """Approved leave per employee; a full-day leave outranks a half-day one covering the same date."""

    def __init__(self, leaves: Iterable[Leave] = ()):
        self._by_employee: dict[Hashable, list[Leave]] = defaultdict(list)
        for leave in leaves:
            if str(leave.status).upper() == "APPROVED":
                self._by_employee[leave.employee].append(leave)

    def on(self, employee: Hashable, day: date) -> Leave | None:
        covering = [leave for leave in self._by_employee.get(employee, ()) if leave.date_from <= day <= leave.date_to]
        if not covering:
            return None
        return next((leave for leave in covering if not leave.is_half_day), covering[0])


def day_context(employee: Hashable, office: Hashable | None, day: date, holidays: HolidayCalendar, leaves: LeaveBook) -> DayContext:
    """The holiday on the employee's posting office (or all offices) and the approved leave for the day."""
    name = holidays.name(office, day)
    leave = leaves.on(employee, day)
    return DayContext(
        is_holiday=name is not None,
        holiday_name=name,
        on_leave=leave is not None,
        leave_is_half_day=bool(leave.is_half_day) if leave is not None else False,
        leave_type=leave.leave_type if leave is not None else None,
    )


@dataclass(frozen=True)
class DeviceLink:
    """``devices_device_user``: one PIN on one device linked to one employee (A1)."""

    device: Hashable
    pin: str
    employee: Hashable


def build_identity_map(links: Iterable[DeviceLink]) -> dict[tuple[Hashable, str], Hashable]:
    """``(device, pin) → employee`` (A1). The same PIN on two terminals is two links, made explicitly; there is no
    global PIN match and no ``employee_code`` fallback. One ``(device, pin)`` linked to two employees is an error."""
    identity: dict[tuple[Hashable, str], Hashable] = {}
    for link in links:
        key = (link.device, str(link.pin).strip())
        if key in identity and identity[key] != link.employee:
            raise ValueError(f"PIN {key[1]!r} on device {key[0]!r} is linked to two employees.")
        identity[key] = link.employee
    return identity


@dataclass(frozen=True)
class RawPunch:
    """An ``attendance_raw_punch`` row as the engine needs it."""

    raw_id: Hashable
    device: Hashable
    pin: str
    punch_at: datetime

    def __post_init__(self):
        _require_aware(self.punch_at, "RawPunch.punch_at")
        object.__setattr__(self, "pin", str(self.pin).strip())


def unmapped_pins(punches: Iterable[RawPunch], identity: Mapping[tuple[Hashable, str], Hashable]) -> dict[Hashable, tuple[str, ...]]:
    """PINs with punches but no link, per device (A1: a PIN linked on another device is still unmapped here)."""
    found: dict[Hashable, set[str]] = defaultdict(set)
    for punch in punches:
        if (punch.device, punch.pin) not in identity:
            found[punch.device].add(punch.pin)
    return {device: tuple(sorted(pins)) for device, pins in found.items()}


# ---------------------------------------------------------------------------------------------------------------
# recomputing a window (v3 process_range, pure)
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Employee:
    """What the engine needs of an employee: posting office, its timezone, the effective shift, employment dates."""

    key: Hashable
    office: Hashable | None = None
    timezone: str = DEFAULT_TIMEZONE
    shift: Shift | None = None
    joined_on: date | None = None
    left_on: date | None = None

    def __post_init__(self):
        zone(self.timezone)

    def employed_on(self, day: date) -> bool:
        return not ((self.joined_on is not None and day < self.joined_on) or (self.left_on is not None and day > self.left_on))


@dataclass(frozen=True)
class DayWrite:
    employee: Hashable
    result: DayResult


@dataclass(frozen=True)
class RecomputeResult:
    writes: tuple[DayWrite, ...]
    skipped_corrected: tuple[tuple[Hashable, date], ...]
    skipped_future: tuple[tuple[Hashable, date], ...]
    unmapped: Mapping[Hashable, tuple[str, ...]]
    raw_punches_considered: int

    @property
    def absent_days(self) -> int:
        return sum(1 for write in self.writes if write.result.status == Status.ABSENT)

    def counts(self) -> dict[str, int]:
        return {
            "written": len(self.writes),
            "skipped_corrected": len(self.skipped_corrected),
            "skipped_future": len(self.skipped_future),
            "absent_days": self.absent_days,
            "raw_punches_considered": self.raw_punches_considered,
            "unmapped_pins": sum(len(pins) for pins in self.unmapped.values()),
        }


def _days(date_from: date, date_to: date) -> Iterable[date]:
    if date_to < date_from:
        raise ValueError("date_to is before date_from.")
    day = date_from
    while day <= date_to:
        yield day
        day += timedelta(days=1)


def recompute(
    *,
    date_from: date,
    date_to: date,
    employees: Sequence[Employee],
    punches: Iterable[RawPunch],
    links: Iterable[DeviceLink] | Mapping[tuple[Hashable, str], Hashable],
    now: datetime,
    holidays: Iterable[Holiday] = (),
    leaves: Iterable[Leave] = (),
    rules: Iterable[AttendanceRule] = (),
    corrected: Iterable[tuple[Hashable, date]] = (),
) -> RecomputeResult:
    """The days to write for ``employees`` over ``[date_from, date_to]`` (v3 ``process_range`` without the database).

    Punches are resolved by ``(device, pin)`` (A1) and attributed to work dates on each employee's office wall clock
    (A6, A10); the caller fetches raw punches from a day before ``date_from`` to a day after ``date_to``. Days before
    joining or after leaving produce nothing; days ≥ today in the employee's office timezone are skipped (A7); corrected
    days are skipped until the correction is revoked (A12). Every other day is written, weekly offs included.
    """
    identity = dict(links) if isinstance(links, Mapping) else build_identity_map(links)
    punches = list(punches)
    staff = {employee.key: employee for employee in employees}
    buckets: dict[Hashable, dict[date, list[Punch]]] = defaultdict(lambda: defaultdict(list))
    for raw in punches:
        employee = staff.get(identity.get((raw.device, raw.pin)))
        if employee is None:
            continue
        work_date = attribute_work_date(local_wall_clock(raw.punch_at, employee.timezone), employee.shift)
        if date_from <= work_date <= date_to:
            buckets[employee.key][work_date].append(Punch(raw_id=raw.raw_id, punch_at=raw.punch_at, device=raw.device))

    holiday_calendar = HolidayCalendar(holidays)
    leave_book = LeaveBook(leaves)
    rule_list = list(rules)
    corrected_days = {(employee, day) for employee, day in corrected}
    today = {employee.key: today_local(now, employee.timezone) for employee in employees}

    writes: list[DayWrite] = []
    skipped_corrected: list[tuple[Hashable, date]] = []
    skipped_future: list[tuple[Hashable, date]] = []
    for day in _days(date_from, date_to):
        for employee in employees:
            if not employee.employed_on(day):
                continue
            if day >= today[employee.key]:
                skipped_future.append((employee.key, day))
                continue
            if (employee.key, day) in corrected_days:
                skipped_corrected.append((employee.key, day))
                continue
            context = day_context(employee.key, employee.office, day, holiday_calendar, leave_book)
            day_rules = rules_for(rule_list, office=employee.office, shift=employee.shift, work_date=day)
            result = compute_day(day, buckets[employee.key][day], employee.shift, context, day_rules, timezone=employee.timezone)
            writes.append(DayWrite(employee=employee.key, result=result))

    return RecomputeResult(
        writes=tuple(writes),
        skipped_corrected=tuple(skipped_corrected),
        skipped_future=tuple(skipped_future),
        unmapped=unmapped_pins(punches, identity),
        raw_punches_considered=len(punches),
    )


def affected_work_dates(punch_instants: Iterable[datetime], *, shift: Shift | None, timezone: str) -> set[date]:
    """The work dates a batch of ingested punches can change for one employee (A8's recompute range)."""
    return {attribute_work_date(local_wall_clock(instant, timezone), shift) for instant in punch_instants}


# ---------------------------------------------------------------------------------------------------------------
# corrections (A12)
# ---------------------------------------------------------------------------------------------------------------

CORRECTABLE_FIELDS = frozenset({"status", "first_in", "last_out", "working_minutes", "break_minutes", "late_minutes", "early_exit_minutes", "overtime_minutes", "is_late", "is_early_exit"})


def apply_correction(result: DayResult, field: str, value: Any) -> DayResult:
    """The day with one field corrected by HR (validated; the stored day is then skipped by recompute)."""
    if field not in CORRECTABLE_FIELDS:
        raise ValueError(f"{field!r} cannot be corrected.")
    if field == "status":
        try:
            value = Status(value)
        except ValueError as exc:
            raise ValueError(f"Unknown status {value!r}.") from exc
    elif field in ("first_in", "last_out"):
        if value is not None:
            _require_naive(value, field)
    elif field in ("is_late", "is_early_exit"):
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be true or false.")
    elif not _is_whole(value) or value < 0:
        raise ValueError(f"{field} must be a whole number of minutes >= 0.")
    corrected = replace(result, **{field: value})
    if corrected.first_in is not None and corrected.last_out is not None and corrected.last_out < corrected.first_in:
        raise ValueError("last_out is before first_in.")
    if corrected.first_in is None and corrected.last_out is not None:
        raise ValueError("A day with an OUT needs an IN.")
    return corrected


# ---------------------------------------------------------------------------------------------------------------
# calendar fill and summaries (A9, v3 calendar_service)
# ---------------------------------------------------------------------------------------------------------------


class Fill(StrEnum):
    STORED = "STORED"  # a processed row exists
    FILLED = "FILLED"  # past day without a row: leave > holiday > weekly off > absent (same rule as compute_day)
    PENDING = "PENDING"  # today or later without a row: blank, never ABSENT (A7)
    NOT_EMPLOYED = "NOT_EMPLOYED"  # before joining or after leaving: blank


@dataclass(frozen=True)
class CalendarDay:
    date: date
    status: str
    fill: str
    first_in: datetime | None = None
    last_out: datetime | None = None
    punch_count: int = 0
    working_minutes: int = 0
    overtime_minutes: int = 0
    late_minutes: int = 0
    early_exit_minutes: int = 0
    worked_on_off_day: bool = False
    leave_conflict: bool = False
    is_corrected: bool = False
    holiday_name: str | None = None
    leave_type: str | None = None

    @property
    def stored(self) -> bool:
        return self.fill == Fill.STORED

    @property
    def status_code(self) -> str:
        return STATUS_CODE.get(self.status, "")

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, "")

    @property
    def missing_out(self) -> bool:
        return is_missing_out(self.first_in, self.last_out)

    @property
    def first_in_label(self) -> str:
        return self.first_in.strftime("%H:%M") if self.first_in is not None else ""

    @property
    def last_out_label(self) -> str:
        """The OUT time, or the words that say there is none — never an invented time, never a bare blank."""
        if self.last_out is not None:
            return self.last_out.strftime("%H:%M")
        return MISSING_OUT_LABEL if self.first_in is not None else ""

    def as_dict(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(
            date=self.date.isoformat(),
            day=self.date.strftime("%a"),
            status=str(self.status),
            fill=str(self.fill),
            stored=self.stored,
            status_code=self.status_code,
            status_label=self.status_label,
            missing_out=self.missing_out,
            first_in=self.first_in_label,
            last_out=self.last_out_label,
            working_hours=hm(self.working_minutes),
        )
        return data


def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    following = date(year + (month == 12), month % 12 + 1, 1)
    return first, following - timedelta(days=1)


def hm(minutes: int | None) -> str:
    """Minutes as ``H:MM``."""
    value = int(minutes or 0)
    return f"{value // 60}:{value % 60:02d}"


def _stored_day(day: date, row: Any, ctx: DayContext) -> CalendarDay:
    return CalendarDay(
        date=day,
        status=str(row.status),
        fill=Fill.STORED,
        first_in=getattr(row, "first_in", None),
        last_out=getattr(row, "last_out", None),
        punch_count=int(getattr(row, "punch_count", 0) or 0),
        working_minutes=int(getattr(row, "working_minutes", 0) or 0),
        overtime_minutes=int(getattr(row, "overtime_minutes", 0) or 0),
        late_minutes=int(getattr(row, "late_minutes", 0) or 0),
        early_exit_minutes=int(getattr(row, "early_exit_minutes", 0) or 0),
        worked_on_off_day=bool(getattr(row, "worked_on_off_day", False)),
        leave_conflict=bool(getattr(row, "leave_conflict", False)),
        is_corrected=bool(getattr(row, "is_corrected", False)),
        holiday_name=ctx.holiday_name,
        leave_type=ctx.leave_type,
    )


def calendar_fill(
    employee: Employee,
    *,
    date_from: date,
    date_to: date,
    now: datetime,
    stored: Mapping[date, Any] | None = None,
    holidays: HolidayCalendar | None = None,
    leaves: LeaveBook | None = None,
) -> list[CalendarDay]:
    """Every date of the range for one employee — the single source of every report and calendar (A9).

    A stored row (any object with the ``attendance_day`` attribute names) is shown as stored. A date without one is
    blank when the person was not employed, blank when it is today or later (A7: v3 filled future working days as
    ABSENT), and otherwise decided exactly as :func:`compute_day` decides a day without punches.
    """
    stored = stored or {}
    holidays = holidays or HolidayCalendar()
    leaves = leaves or LeaveBook()
    today = today_local(now, employee.timezone)
    days: list[CalendarDay] = []
    for day in _days(date_from, date_to):
        ctx = day_context(employee.key, employee.office, day, holidays, leaves)
        row = stored.get(day)
        if row is not None:
            days.append(_stored_day(day, row, ctx))
        elif not employee.employed_on(day):
            days.append(CalendarDay(date=day, status="", fill=Fill.NOT_EMPLOYED))
        elif day >= today:
            days.append(CalendarDay(date=day, status="", fill=Fill.PENDING, holiday_name=ctx.holiday_name, leave_type=ctx.leave_type))
        else:
            status = _no_punch_status(ctx, is_working_day(day, employee.shift))
            days.append(CalendarDay(date=day, status=status, fill=Fill.FILLED, holiday_name=ctx.holiday_name, leave_type=ctx.leave_type))
    return days


RATE_QUANTUM = Decimal("0.0001")


def attendance_rate(*, present: int, late: int, half_day: int, absent: int) -> Decimal | None:
    """A9: ``(present + 0.5 × half) / expected`` as a fraction, where present counts PRESENT and LATE days and
    expected = PRESENT + LATE + HALF_DAY + ABSENT (leave, holidays and weekly offs are not expected days)."""
    expected = present + late + half_day + absent
    if expected == 0:
        return None
    attended = Decimal(present + late) + Decimal(half_day) / 2
    return (attended / Decimal(expected)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


_COUNT_KEYS = (
    "present",
    "late",
    "absent",
    "half_day",
    "weekly_off",
    "holiday",
    "leave",
    "present_days",
    "absent_days",
    "expected_days",
    "worked_off_days",
    "leave_conflicts",
    "pending_days",
    "not_employed_days",
    "working_minutes",
    "overtime_minutes",
)


def _finish(summary: dict[str, Any]) -> dict[str, Any]:
    summary["attendance_rate"] = attendance_rate(present=summary["present"], late=summary["late"], half_day=summary["half_day"], absent=summary["absent"])
    summary["working_hours"] = hm(summary["working_minutes"])
    summary["overtime_hours"] = hm(summary["overtime_minutes"])
    return summary


def summarise(days: Iterable[CalendarDay]) -> dict[str, Any]:
    """Counts by status plus totals, from the days actually shown (v3 ``summarise`` + A9)."""
    by_status = {status: 0 for status in Status}
    summary = dict.fromkeys(_COUNT_KEYS, 0)
    for day in days:
        if day.status in by_status:
            by_status[Status(day.status)] += 1
        summary["worked_off_days"] += int(day.worked_on_off_day)
        summary["leave_conflicts"] += int(day.leave_conflict)
        summary["pending_days"] += int(day.fill == Fill.PENDING)
        summary["not_employed_days"] += int(day.fill == Fill.NOT_EMPLOYED)
        summary["working_minutes"] += day.working_minutes
        summary["overtime_minutes"] += day.overtime_minutes
    summary.update(
        present=by_status[Status.PRESENT],
        late=by_status[Status.LATE],
        absent=by_status[Status.ABSENT],
        half_day=by_status[Status.HALF_DAY],
        weekly_off=by_status[Status.WEEKLY_OFF],
        holiday=by_status[Status.HOLIDAY],
        leave=by_status[Status.ON_LEAVE],
    )
    summary["present_days"] = summary["present"] + summary["late"] + summary["half_day"]
    summary["absent_days"] = summary["absent"]
    summary["expected_days"] = summary["present_days"] + summary["absent"]
    return _finish(summary)


def combine_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """An office (or any group) from its members' summaries, with the same rate formula over the pooled counts."""
    total = dict.fromkeys(_COUNT_KEYS, 0)
    for summary in summaries:
        for key in _COUNT_KEYS:
            total[key] += int(summary[key])
    return _finish(total)


def totals_by_date(calendars: Sequence[Sequence[CalendarDay]]) -> list[dict[str, Any]]:
    """Per-date column totals of a roster (every calendar covers the same dates), counted from the drawn days."""
    if not calendars:
        return []
    columns = []
    for index, first in enumerate(calendars[0]):
        column: dict[str, Any] = {"date": first.date.isoformat(), "day": first.date.strftime("%a")}
        cells = [calendar[index] for calendar in calendars]
        if any(cell.date != first.date for cell in cells):
            raise ValueError("Every calendar must cover the same dates.")
        for status in Status:
            column[status.value.lower()] = sum(1 for cell in cells if cell.status == status)
        column["stored"] = sum(1 for cell in cells if cell.stored)
        columns.append(column)
    return columns


def context_lookup(holidays: HolidayCalendar, leaves: LeaveBook, employee: Employee) -> Callable[[date], DayContext]:
    """``day → DayContext`` for one employee (for callers that build contexts outside :func:`recompute`)."""
    return lambda day: day_context(employee.key, employee.office, day, holidays, leaves)
