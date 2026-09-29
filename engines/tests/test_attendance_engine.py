"""engines.attendance: the ported eSSL cases (v4 expectations) and the explicit A-row, overnight, break, debounce and
timezone behaviour. Recompute/calendar behaviour is in test_attendance_recompute.py / test_attendance_calendar.py."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engines import attendance as att
from engines.tests import attendance_cases as cases
from engines.tests.attendance_support import IST, aware, run_case, shift

DAY = cases.DAY
GEN = cases.GEN


def _punch(raw_id, clock, day=DAY, device=None, tz=IST):
    parts = [int(p) for p in clock.split(":")]
    return att.Punch(raw_id=raw_id, punch_at=datetime.combine(day, time(*parts), tzinfo=tz), device=device)


# ---------------------------------------------------------------------------------------------------------------
# the ported cases
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", cases.CASES, ids=lambda case: case.id)
def test_ported_case(case):
    result = run_case(case)
    for name, expected in case.expect.items():
        actual = getattr(result, name)
        assert actual == expected, f"{case.id}.{name}: {actual!r} != {expected!r}"
    assert result.processing_version == "v4"
    assert set(result.source_raw_ids) | set(result.ignored_raw_ids) == {raw_id for raw_id, _, _ in case.punch_list()}


def test_every_a_row_that_changes_compute_results_has_a_case():
    tagged = {row for case in cases.CASES for row in case.changed_by}
    assert {"A2", "A3", "A4", "A5"} <= tagged
    assert {row for case in cases.ATTRIBUTIONS for row in case.changed_by} == {"A6"}


def test_every_ported_source_file_is_represented():
    sources = {case.source.split("::")[0] for case in cases.CASES}
    for name in (
        "test_attendance_processing.py",
        "test_half_day_and_in_out.py",
        "test_employee_list_active_default.py",
        "test_multi_device_attendance.py",
        "attendance_logic_test.py",
        "spec §C5",
    ):
        assert name in sources


# ---------------------------------------------------------------------------------------------------------------
# A2 — break deduction is continuous and monotonic
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [GEN, {**GEN, "break_minutes": 30}, {**GEN, "break_minutes": 600}, {**GEN, "half_day_minutes": 0}, {**GEN, "break_minutes": 0}],
    ids=["default", "break30", "break600", "half0", "nobreak"],
)
def test_break_deduction_is_continuous_and_monotonic(overrides):
    the_shift = shift(overrides)
    previous = 0
    for gross in range(0, 1200):
        working, deducted = att.deduct_break(gross, the_shift)
        assert working + deducted == gross
        assert 0 <= deducted <= the_shift.break_minutes
        assert 0 <= working - previous <= 1, f"jump at {gross}"
        previous = working


def test_break_boundaries_of_the_default_shift():
    gen = shift(GEN)
    assert att.deduct_break(60, gen) == (60, 0)
    assert att.deduct_break(61, gen) == (61, 0)  # v3: 1 worked
    assert att.deduct_break(240, gen) == (240, 0)
    assert att.deduct_break(270, gen) == (240, 30)
    assert att.deduct_break(300, gen) == (240, 60)
    assert att.deduct_break(540, gen) == (480, 60)
    assert att.deduct_break(-5, gen) == (0, 0)


def test_no_break_without_a_shift_or_when_switched_off():
    assert att.deduct_break(540, None) == (540, 0)
    assert att.deduct_break(540, shift({**GEN, "auto_deduct_break": False})) == (540, 0)


# ---------------------------------------------------------------------------------------------------------------
# A3 — debounce
# ---------------------------------------------------------------------------------------------------------------


def test_debounce_compares_with_the_previous_accepted_punch():
    punches = [_punch(1, "09:00:00"), _punch(2, "09:01:59"), _punch(3, "09:02:00"), _punch(4, "09:03:59"), _punch(5, "09:04:01")]
    accepted, ignored = att.debounce(punches, 2)
    assert [p.raw_id for p in accepted] == [1, 3, 5]
    assert [p.raw_id for p in ignored] == [2, 4]


def test_debounce_sorts_and_spans_devices():
    punches = [_punch(1, "18:00", device="GATE"), _punch(2, "09:00:40", device="LOBBY"), _punch(3, "09:00:00", device="GATE")]
    accepted, ignored = att.debounce(punches, 2)
    assert [p.raw_id for p in accepted] == [3, 1]
    assert [p.raw_id for p in ignored] == [2]


def test_debounce_disabled_keeps_everything():
    punches = [_punch(1, "09:00"), _punch(2, "09:00")]
    assert att.debounce(punches, 0) == (tuple(punches), ())


def test_debounce_applies_without_a_shift():
    result = att.compute_day(DAY, [_punch(1, "09:00:00"), _punch(2, "09:00:20")], None)
    assert result.punch_count == 1 and result.ignored_raw_ids == (2,)
    assert result.missing_out is True


def test_a_debounced_day_keeps_its_raw_rows_traceable():
    result = att.compute_day(DAY, [_punch(7, "09:00:00"), _punch(8, "09:00:30"), _punch(9, "18:00")], shift(GEN))
    assert result.source_raw_ids == (7, 9)
    assert result.ignored_raw_ids == (8,)
    assert result.as_dict()["ignored_raw_ids"] == [8]


# ---------------------------------------------------------------------------------------------------------------
# A4 — punches on holidays, weekly offs and leave
# ---------------------------------------------------------------------------------------------------------------


def test_worked_holiday_is_overtime_and_never_late_or_half():
    result = att.compute_day(DAY, [_punch(1, "11:30"), _punch(2, "15:00")], shift(GEN), att.DayContext(is_holiday=True, holiday_name="Onam"))
    assert result.status == att.Status.HOLIDAY
    assert result.worked_on_off_day is True
    assert (result.overtime_minutes, result.working_minutes) == (210, 210)
    assert (result.late_minutes, result.early_exit_minutes, result.is_late, result.is_early_exit) == (0, 0, False, False)


def test_worked_weekly_off_without_a_shift_is_sunday_overtime():
    result = att.compute_day(cases.SUNDAY, [_punch(1, "10:00", cases.SUNDAY), _punch(2, "12:00", cases.SUNDAY)], None)
    assert result.status == att.Status.WEEKLY_OFF and result.overtime_minutes == 120


def test_full_day_leave_with_punches_is_flagged_not_hidden():
    late = att.compute_day(DAY, [_punch(1, "10:30"), _punch(2, "15:00")], shift(GEN), att.DayContext(on_leave=True, leave_type="CL"))
    assert late.status == att.Status.HALF_DAY and late.leave_conflict is True
    half_leave = att.compute_day(DAY, [_punch(1, "10:30"), _punch(2, "15:00")], shift(GEN), att.DayContext(on_leave=True, leave_is_half_day=True))
    assert half_leave.status == att.Status.HALF_DAY and half_leave.leave_conflict is False


def test_no_punch_precedence_leave_holiday_weekly_off_absent():
    gen = shift(GEN)
    assert att.compute_day(cases.SUNDAY, [], gen, att.DayContext(on_leave=True)).status == att.Status.ON_LEAVE
    assert att.compute_day(cases.SUNDAY, [], gen, att.DayContext(is_holiday=True)).status == att.Status.HOLIDAY
    assert att.compute_day(cases.SUNDAY, [], gen).status == att.Status.WEEKLY_OFF
    assert att.compute_day(DAY, [], gen).status == att.Status.ABSENT


# ---------------------------------------------------------------------------------------------------------------
# A5 — half-day deadline and rules
# ---------------------------------------------------------------------------------------------------------------


def test_default_deadline_is_start_plus_the_shift_allowance():
    assert att.half_day_deadline(DAY, shift(GEN)) == datetime(2026, 9, 1, 9, 30)
    assert att.half_day_deadline(DAY, shift({**GEN, "half_day_after_minutes": 45})) == datetime(2026, 9, 1, 9, 45)
    assert att.half_day_deadline(DAY, shift(cases.NIGHT)) == datetime(2026, 9, 1, 22, 30)
    assert att.half_day_deadline(DAY, None) is None


def test_describe_half_day_deadline_names_its_source():
    gen = shift(GEN)
    assert att.describe_half_day_deadline(gen, None, DAY) == (time(9, 30), "shift")
    assert att.describe_half_day_deadline(gen, att.DayRules(half_day_after=time(11, 0)), DAY) == (time(11, 0), "rule")
    assert att.describe_half_day_deadline(gen, att.DayRules(half_day_after_minutes=90), DAY) == (time(10, 30), "rule")


def test_rules_scope_and_effective_dates():
    gen = shift(GEN)
    rules = [
        att.AttendanceRule(rules={"half_day_under_minutes": 200}),
        att.AttendanceRule(rules={"half_day_after": "11:00"}, office="O1", effective_from=date(2026, 1, 1)),
        att.AttendanceRule(rules={"half_day_after": "12:00"}, office="O2"),
        att.AttendanceRule(rules={"half_day_after": "13:00"}, shift="OTHER"),
        att.AttendanceRule(rules={"half_day_after": "14:00"}, office="O1", is_active=False),
        att.AttendanceRule(rules={"half_day_after": "15:00"}, office="O1", effective_from=date(2026, 10, 1)),
    ]
    resolved = att.rules_for(rules, office="O1", shift=gen, work_date=DAY)
    assert resolved == att.DayRules(half_day_after=time(11, 0), half_day_under_minutes=200)
    assert att.rules_for(rules, office=None, shift=None, work_date=DAY) == att.DayRules(half_day_under_minutes=200)


def test_rules_from_payload_ignores_what_it_does_not_recognise():
    parsed = att.rules_from_payload({"half_day_after": time(10, 5), "half_day_after_minutes": 30.0, "half_day_under_minutes": True, "other": 1})
    assert parsed == att.DayRules(half_day_after=time(10, 5), half_day_after_minutes=30)
    assert att.rules_from_payload({"half_day_after_minutes": "45", "half_day_under_minutes": 2.5}) == att.DayRules(half_day_after_minutes=45)
    assert att.rules_from_payload({"half_day_after_minutes": "abc", "half_day_under_minutes": [30]}) == att.DayRules()
    assert att.rules_from_payload(None) == att.DayRules()


def test_leaving_within_the_early_exit_threshold_is_not_an_early_exit():
    result = att.compute_day(DAY, [_punch(1, "09:00"), _punch(2, "17:46")], shift(GEN))
    assert (result.is_early_exit, result.early_exit_minutes) == (False, 0)
    result = att.compute_day(DAY, [_punch(1, "09:00"), _punch(2, "17:45")], shift(GEN))
    assert (result.is_early_exit, result.early_exit_minutes) == (True, 15)


def test_validate_rules_payload():
    assert att.validate_rules_payload({"half_day_after": "10:30", "half_day_after_minutes": 30, "half_day_under_minutes": 0}) == {}
    errors = att.validate_rules_payload({"half_day_after": "late", "half_day_after_minutes": -1, "half_day_under_minutes": "60", "colour": "red"})
    assert set(errors) == {"half_day_after", "half_day_after_minutes", "half_day_under_minutes", "colour"}
    assert att.validate_rules_payload(["half_day_after"]) == {"rules": "Must be an object."}


@pytest.mark.parametrize("key", ["half_day_after_minutes", "half_day_under_minutes"])
def test_rule_minutes_beyond_a_day_are_rejected_and_never_break_a_recompute(key):
    """``hr_attendance_rule.rules`` is JSON: nothing but the validator bounds a number. A global/office rule with an
    allowance of 10**10 minutes made every compute_day in its scope raise OverflowError (the deadline lands past year
    9999), so one bad rule stopped the recompute and finalise_day for a whole office."""
    huge = 10**10
    assert set(att.validate_rules_payload({key: huge})) == {key}
    assert set(att.validate_rules_payload({key: att.MAX_RULE_MINUTES + 1})) == {key}
    assert att.validate_rules_payload({key: att.MAX_RULE_MINUTES}) == {}
    assert att.rules_from_payload({key: huge}) == att.DayRules()  # out of range = malformed = ignored (v3)
    gen = shift(GEN)
    rules = att.rules_for([att.AttendanceRule(rules={key: huge})], office=None, shift=gen, work_date=DAY)
    assert rules == att.DayRules()
    result = att.compute_day(DAY, [_punch(1, "10:00"), _punch(2, "15:00")], gen, rules=rules)
    assert result.status == att.Status.HALF_DAY  # the shift's own 09:30 deadline still applies


def test_malformed_rule_documents_are_ignored_not_raised():
    """A rules document that is not an object (imported JSON, a list) is ignored like any malformed value."""
    assert att.rules_from_payload(["half_day_after", "11:00"]) == att.DayRules()
    assert att.rules_from_payload("11:00") == att.DayRules()
    rules = att.rules_for([att.AttendanceRule(rules=["half_day_after"]), att.AttendanceRule(rules={"half_day_after": "11:00"}, office="O1")], office="O1", shift=shift(GEN), work_date=DAY)
    assert rules == att.DayRules(half_day_after=time(11, 0))
    assert att.rules_from_payload({"half_day_after_minutes": Decimal("30.5"), "half_day_under_minutes": Decimal("Infinity")}) == att.DayRules()
    assert att.rules_from_payload({"half_day_after_minutes": Decimal("45")}) == att.DayRules(half_day_after_minutes=45)


def test_under_minutes_still_needs_a_late_arrival():
    gen = shift(GEN)
    rules = att.DayRules(half_day_under_minutes=300)
    late_long = att.compute_day(DAY, [_punch(1, "10:00"), _punch(2, "17:00")], gen, rules=rules)  # 360 credited ≥ 300
    late_short = att.compute_day(DAY, [_punch(1, "10:00"), _punch(2, "14:00")], gen, rules=rules)  # 240 < 300
    late_single = att.compute_day(DAY, [_punch(1, "10:00")], gen, rules=rules)  # no span: not a short day
    assert (late_long.status, late_short.status, late_single.status) == (att.Status.LATE, att.Status.HALF_DAY, att.Status.LATE)


def test_grace_credit_counts_toward_a_full_day_only_for_the_status():
    seed = shift(cases.SEED)
    # 09:35-18:30: 475 worked + 5 minutes of grace = a full day; the reported hours stay 475.
    result = att.compute_day(DAY, [_punch(1, "09:35"), _punch(2, "18:30")], seed, rules=att.DayRules(half_day_after_minutes=0))
    assert result.status == att.Status.PRESENT and result.working_minutes == 475


# ---------------------------------------------------------------------------------------------------------------
# A6 — overnight shifts
# ---------------------------------------------------------------------------------------------------------------


def test_overnight_shift_boundaries_and_cutoff():
    night = shift(cases.NIGHT)
    assert att.shift_boundaries(DAY, night) == (datetime(2026, 9, 1, 22, 0), datetime(2026, 9, 2, 6, 0))
    assert att.overnight_cutoff(night) == 9 * 3600
    assert att.overnight_cutoff(shift({**cases.NIGHT, "overnight_buffer_minutes": 0})) == 6 * 3600


def test_a_mis_set_day_shift_rolls_its_end_forward():
    assert att.shift_boundaries(DAY, shift({"start_time": time(20, 0), "end_time": time(4, 0)})) == (datetime(2026, 9, 1, 20, 0), datetime(2026, 9, 2, 4, 0))


def test_overnight_late_early_and_overtime():
    night = shift(cases.NIGHT)
    result = att.compute_day(DAY, [_punch(1, "22:20"), _punch(2, "05:00", cases.NEXT_DAY)], night)
    assert (result.late_minutes, result.is_late) == (10, True)
    assert (result.early_exit_minutes, result.is_early_exit) == (60, True)
    assert result.first_in == datetime(2026, 9, 1, 22, 20) and result.last_out == datetime(2026, 9, 2, 5, 0)
    assert result.status == att.Status.LATE


# ---------------------------------------------------------------------------------------------------------------
# A10 — office timezone, clocks; A11 — dedup key
# ---------------------------------------------------------------------------------------------------------------


def test_punches_are_read_on_the_office_wall_clock():
    utc_in = att.Punch(1, datetime(2026, 9, 1, 3, 30, tzinfo=UTC))  # 09:00 IST
    utc_out = att.Punch(2, datetime(2026, 9, 1, 12, 30, tzinfo=UTC))  # 18:00 IST
    ist = att.compute_day(DAY, [utc_in, utc_out], shift(GEN), timezone="Asia/Kolkata")
    assert (ist.first_in, ist.last_out, ist.status, ist.working_minutes) == (datetime(2026, 9, 1, 9, 0), datetime(2026, 9, 1, 18, 0), att.Status.PRESENT, 480)
    dubai = att.compute_day(DAY, [utc_in, utc_out], shift(GEN), timezone="Asia/Dubai")  # 07:30 → 16:30 there
    assert (dubai.first_in, dubai.last_out, dubai.is_early_exit, dubai.early_exit_minutes) == (datetime(2026, 9, 1, 7, 30), datetime(2026, 9, 1, 16, 30), True, 90)


def test_elapsed_time_is_real_across_a_dst_change():
    berlin = ZoneInfo("Europe/Berlin")  # 2026-10-25: clocks go back at 03:00
    day = date(2026, 10, 25)
    punches = [att.Punch(1, datetime(2026, 10, 25, 1, 0, tzinfo=berlin)), att.Punch(2, datetime(2026, 10, 25, 5, 0, tzinfo=berlin))]
    result = att.compute_day(day, punches, None, timezone="Europe/Berlin")
    assert result.working_minutes == 300  # five real hours between 01:00 and 05:00 wall clock


def test_timezone_names_are_validated():
    assert att.is_valid_timezone("Asia/Kolkata") and not att.is_valid_timezone("Mars/Olympus") and not att.is_valid_timezone(None)
    for bad in ("", "Mars/Olympus", "../etc/passwd", None):
        with pytest.raises(ValueError):
            att.zone(bad)
    with pytest.raises(ValueError):
        att.compute_day(DAY, [], None, timezone="Nowhere/Here")
    with pytest.raises(ValueError):
        att.Employee(key=1, timezone="Nowhere/Here")


def test_today_is_the_office_date():
    late_evening_utc = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)  # 00:30 on 2 September in India
    assert att.today_local(late_evening_utc, "Asia/Kolkata") == date(2026, 9, 2)
    assert att.today_local(late_evening_utc, "UTC") == date(2026, 9, 1)
    assert att.finalise_target(late_evening_utc, "Asia/Kolkata") == date(2026, 9, 1)
    with pytest.raises(ValueError):
        att.today_local(datetime(2026, 9, 1, 19, 0), "UTC")


def test_device_time_becomes_punch_at_without_correcting_the_clock():
    punch_at = att.punch_at_from_device_time(datetime(2026, 9, 1, 9, 0), "Asia/Kolkata")
    assert punch_at == datetime(2026, 9, 1, 3, 30, tzinfo=UTC)
    assert att.local_wall_clock(punch_at, "Asia/Kolkata") == datetime(2026, 9, 1, 9, 0)
    with pytest.raises(ValueError):
        att.punch_at_from_device_time(punch_at, "Asia/Kolkata")


def test_clock_offset_is_measured_in_seconds():
    server_now = datetime(2026, 9, 1, 3, 30, 0, tzinfo=UTC)  # 09:00:00 IST
    assert att.clock_offset_seconds(datetime(2026, 9, 1, 8, 58, 36), server_now, "Asia/Kolkata") == -84
    assert att.clock_offset_seconds(datetime(2026, 9, 1, 9, 11, 0), server_now, "Asia/Kolkata") == 660


def test_dedup_key_is_a_content_hash_across_transports():
    moment = datetime(2026, 9, 1, 9, 3)
    adms = att.punch_dedup_key("NCD8253601138", "1", moment, 15, 255)
    agent = att.punch_dedup_key(" ncd8253601138 ", 1, moment, 15, 255)
    assert adms == agent and len(adms) == 64
    assert adms != att.punch_dedup_key("NCD8253601139", "1", moment, 15, 255)  # another terminal: another punch
    assert adms != att.punch_dedup_key("NCD8253601138", "2", moment, 15, 255)
    assert adms != att.punch_dedup_key("NCD8253601138", "1", moment + timedelta(seconds=1), 15, 255)
    assert adms != att.punch_dedup_key("NCD8253601138", "1", moment, 4, 255)
    assert adms != att.punch_dedup_key("NCD8253601138", "1", moment, 15, 0)
    assert att.punch_dedup_key("S", "1", moment) != att.punch_dedup_key("S", "1", moment, 0, 0)
    for bad in (
        lambda: att.punch_dedup_key("S", "1", moment.replace(tzinfo=UTC)),
        lambda: att.punch_dedup_key("", "1", moment),
        lambda: att.punch_dedup_key("S", " ", moment),
    ):
        with pytest.raises(ValueError):
            bad()


# ---------------------------------------------------------------------------------------------------------------
# data validation and purity
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"start_time": "09:00"},
        {"end_time": datetime(2026, 9, 1, 18, 0)},
        {"grace_minutes": -1},
        {"break_minutes": 1.5},
        {"debounce_minutes": True},
        {"working_days": [0, 7]},
        {"weekly_off_days": ["6"]},
        {"start_time": time(9, 0, tzinfo=UTC)},
    ],
)
def test_shift_is_validated(bad):
    with pytest.raises(ValueError):
        att.Shift(**{**GEN, **bad})


def test_shift_accepts_json_lists_and_empty_working_days():
    the_shift = att.Shift(**{**GEN, "working_days": [], "weekly_off_days": None})
    assert the_shift.working_days == () and the_shift.weekly_off_days == ()
    assert att.is_working_day(cases.SUNDAY, the_shift) is True
    assert att.is_working_day(cases.SUNDAY, att.Shift(**{**GEN, "working_days": [0, 1, 2, 3, 4, 5, 6]})) is False  # weekly off wins


def test_punches_must_be_aware_and_typed():
    with pytest.raises(ValueError):
        att.Punch(1, datetime(2026, 9, 1, 9, 0))
    with pytest.raises(ValueError):
        att.RawPunch(1, "D", "1", datetime(2026, 9, 1, 9, 0))
    with pytest.raises(TypeError):
        att.compute_day(DAY, [object()], None)


def test_effective_shift_prefers_the_employee_shift():
    own, default = shift(GEN), shift(cases.SEED)
    assert att.effective_shift(own, default) is own
    assert att.effective_shift(None, default) is default
    assert att.effective_shift(None, None) is None


def test_compute_day_is_deterministic_and_leaves_its_inputs_alone():
    punches = [_punch(1, "18:00"), _punch(2, "09:00"), _punch(3, "09:00:30")]
    snapshot = list(punches)
    first = att.compute_day(DAY, punches, shift(GEN))
    second = att.compute_day(DAY, punches, shift(GEN))
    assert first == second
    assert punches == snapshot


def test_day_result_as_dict():
    result = att.compute_day(DAY, [_punch(1, "09:00")], shift(GEN))
    data = result.as_dict()
    assert data["status"] == "PRESENT" and data["missing_out"] is True and data["source_raw_ids"] == [1] and data["processing_version"] == "v4"


def test_counts_as_present_and_missing_out_helpers():
    assert [att.counts_as_present(s) for s in ("PRESENT", "LATE", "HALF_DAY", "ABSENT", "ON_LEAVE", None)] == [True, True, True, False, False, False]
    assert att.is_missing_out(datetime(2026, 9, 1, 9, 0), None) is True
    assert att.is_missing_out(None, None) is False


def test_engine_imports_nothing_from_django_or_the_apps():
    for module in ("attendance", "inspection_checks", "inspection_readiness"):
        tree = ast.parse((Path(att.__file__).parent / f"{module}.py").read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert imported <= {"__future__", "hashlib", "collections", "dataclasses", "datetime", "decimal", "enum", "typing", "zoneinfo", "math", "engines"}, imported


def test_aware_helper():
    assert aware(datetime(2026, 9, 1, 9, 0)).utcoffset() == timedelta(hours=5, minutes=30)
