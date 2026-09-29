"""hr ↔ the attendance engine (engines-ops), wired at the wave-1 integration.

hr owns the shifts and the attendance rules; ``engines.attendance`` computes the days from them. Both must agree on
what a working day is and on which rule documents are valid (engines-ops hand-over: "reject rule minutes above
MAX_RULE_MINUTES at the attendance-rules/ API").
"""

import datetime as dt

import pytest

from core.errors import DomainError
from engines import attendance as engine
from hr.services.common import is_working_day
from hr.services.rules import validate_rules
from hr.tests.factories import ShiftFactory

WEEK = [dt.date(2026, 9, 28) + dt.timedelta(days=offset) for offset in range(7)]  # Monday … Sunday


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("working_days", "weekly_off_days"),
    [([0, 1, 2, 3, 4, 5], [6]), ([], [6]), ([], []), ([0, 1, 2, 3, 4], [4, 5, 6]), ([0, 2, 4], [])],
)
def test_the_working_day_rule_is_the_engines(working_days, weekly_off_days):
    shift = ShiftFactory(working_days=working_days, weekly_off_days=weekly_off_days)
    engine_shift = engine.Shift(start_time=shift.start_time, end_time=shift.end_time, working_days=tuple(working_days), weekly_off_days=tuple(weekly_off_days))
    assert [is_working_day(day, shift) for day in WEEK] == [engine.is_working_day(day, engine_shift) for day in WEEK]
    assert [is_working_day(day, None) for day in WEEK] == [engine.is_working_day(day, None) for day in WEEK] == [True] * 6 + [False]


@pytest.mark.parametrize(
    "rules",
    [
        {"half_day_after": "10:00"},
        {"half_day_after": "10:00:30"},
        {"half_day_after_minutes": 0},
        {"half_day_after_minutes": 720},
        {"half_day_under_minutes": 0},
        {"half_day_under_minutes": engine.MAX_RULE_MINUTES},
        {"half_day_after": "09:45", "half_day_after_minutes": 45, "half_day_under_minutes": 300},
    ],
)
def test_every_rule_document_hr_accepts_is_valid_for_the_engine(rules):
    stored = validate_rules(rules)
    assert engine.validate_rules_payload(stored) == {}
    parsed = engine.rules_from_payload(stored)
    for key in ("half_day_after_minutes", "half_day_under_minutes"):
        assert getattr(parsed, key) == stored.get(key)
    if "half_day_after" in stored:
        assert parsed.half_day_after == dt.time.fromisoformat(stored["half_day_after"])


@pytest.mark.parametrize(
    "rules",
    [
        {"half_day_under_minutes": engine.MAX_RULE_MINUTES + 1},
        {"half_day_after_minutes": 30.5},
        {"half_day_after_minutes": "30"},
        {"half_day_after_minutes": -1},
        {"half_day_after": "25:00"},
        {"late_after": 10},
    ],
)
def test_what_the_engine_would_ignore_hr_refuses(rules):
    assert engine.validate_rules_payload(rules) != {}
    with pytest.raises(DomainError):
        validate_rules(rules)
