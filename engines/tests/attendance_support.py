"""Builders shared by the attendance engine tests: case table → v4 engine inputs."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from engines import attendance as att
from engines.tests.attendance_cases import TIMEZONE, Case

OFFICE = "O1"
SHIFT_KEY = "S1"
IST = ZoneInfo(TIMEZONE)


def aware(wall_clock: datetime, timezone: str = TIMEZONE) -> datetime:
    return wall_clock.replace(tzinfo=ZoneInfo(timezone))


def shift(overrides: dict | None) -> att.Shift | None:
    return None if overrides is None else att.Shift(key=SHIFT_KEY, **overrides)


def rule_rows(rules: tuple) -> list[att.AttendanceRule]:
    return [
        att.AttendanceRule(
            rules=rule["rules"],
            office=OFFICE if rule["scope"] == "office" else None,
            shift=SHIFT_KEY if rule["scope"] == "shift" else None,
            effective_from=rule.get("effective_from"),
            is_active=rule.get("is_active", True),
        )
        for rule in rules
    ]


def day_rules(rules: tuple, the_shift: att.Shift | None, work_date) -> att.DayRules:
    return att.rules_for(rule_rows(rules), office=OFFICE, shift=the_shift, work_date=work_date)


def punches(case: Case) -> list[att.Punch]:
    return [att.Punch(raw_id=raw_id, punch_at=aware(moment), device=device) for raw_id, moment, device in case.punch_list()]


def run_case(case: Case) -> att.DayResult:
    the_shift = shift(case.shift)
    return att.compute_day(case.day, punches(case), the_shift, att.DayContext(**case.context), day_rules(case.rules, the_shift, case.day), timezone=TIMEZONE)


def comparable(value):
    """A value as the golden JSON holds it."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [comparable(item) for item in value]
    if isinstance(value, att.Status):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
