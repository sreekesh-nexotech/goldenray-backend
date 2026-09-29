"""Compute-level cases of the eSSL v3 test-suite, ported to the v4 engine (engines.attendance).

Sources (eSSL ``backend/``): ``tests/test_attendance_processing.py`` (proc.*), ``tests/test_half_day_and_in_out.py``
(half.*), ``tests/test_employee_list_active_default.py`` (cutoff.*), ``tests/test_multi_device_attendance.py``
(multi.*), ``scripts/attendance_logic_test.py`` (logic.*), the worked examples of the eSSL spec §C5 (worked.*) and
the explicit v4 cases (v4.*). ``test_status_reports_calendar.py`` has no day-computation case of its own; its calendar
cases are in ``test_attendance_calendar.py``.

Each case states the v4 expectation. A case whose result v4 changes names the A-rows responsible in ``changed_by``
and keeps the v3 value of every changed field in ``v3`` (and in a comment). ``test_attendance_parity.py`` checks both
against ``golden/essl_v3_attendance.json`` — the real v3 engine's output for every case, captured by
``scripts/parity/capture_essl_v3.py`` — and requires every untagged case to be identical to v3.

Punch times are office-local wall clock (Asia/Kolkata); raw ids are 1…n in the order listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

TIMEZONE = "Asia/Kolkata"

DAY = date(2026, 9, 1)  # a Tuesday: a working day on every fixture shift
NEXT_DAY = DAY + timedelta(days=1)
SUNDAY = date(2026, 9, 6)
WORKDAY = date(2026, 8, 17)  # attendance_logic_test.py: a Monday
LOGIC_SUNDAY = date(2026, 8, 23)

#: eSSL tests/conftest.py ``shift`` fixture: GEN 09:00–18:00; every other field equals the v4 shift default.
GEN: dict[str, Any] = {"start_time": time(9, 0), "end_time": time(18, 0)}
#: The seed shift of the spec's worked examples and attendance_logic_test.py ``make_shift``: 09:30–18:30.
SEED: dict[str, Any] = {"start_time": time(9, 30), "end_time": time(18, 30)}
NIGHT: dict[str, Any] = {"start_time": time(22, 0), "end_time": time(6, 0), "is_overnight": True}

#: The shift fields v3 also has, with the fixture/v4 defaults (used by the capture script to build the v3 shift).
V3_SHIFT_DEFAULTS: dict[str, Any] = {
    "is_overnight": False,
    "grace_minutes": 10,
    "late_threshold_minutes": 0,
    "early_exit_threshold_minutes": 15,
    "full_day_minutes": 480,
    "half_day_minutes": 240,
    "break_minutes": 60,
    "auto_deduct_break": True,
    "overtime_enabled": True,
    "overtime_after_minutes": 480,
    "working_days": (0, 1, 2, 3, 4, 5),
    "weekly_off_days": (6,),
}

#: Fields both engines produce; the parity test compares exactly these.
COMPARED_FIELDS = (
    "status",
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
    "source_raw_ids",
)


def at(day: date, clock: str) -> datetime:
    parts = [int(part) for part in clock.split(":")]
    return datetime.combine(day, time(*parts))


@dataclass(frozen=True)
class Case:
    id: str
    source: str
    day: date
    punches: tuple = ()
    expect: dict = field(default_factory=dict)
    shift: dict | None = field(default_factory=lambda: dict(GEN))
    context: dict = field(default_factory=dict)
    rules: tuple = ()
    v3: dict = field(default_factory=dict)
    changed_by: tuple = ()

    def punch_list(self) -> list[tuple[int, datetime, str | None]]:
        """``(raw_id, wall clock, device)`` for every punch."""
        out = []
        for raw_id, item in enumerate(self.punches, start=1):
            moment, device = item if isinstance(item, tuple) else (item, None)
            out.append((raw_id, at(self.day, moment) if isinstance(moment, str) else moment, device))
        return out


HOLIDAY = {"is_holiday": True, "holiday_name": "Onam"}
LEAVE = {"on_leave": True, "leave_type": "CASUAL"}
HALF_LEAVE = {"on_leave": True, "leave_is_half_day": True, "leave_type": "CASUAL"}

CASES: tuple[Case, ...] = (
    # --- test_attendance_processing.py ------------------------------------------------------------------------
    Case(
        "proc.first_punch_in_last_out",
        "test_attendance_processing.py::test_first_punch_is_in_and_last_punch_is_out",
        DAY,
        ("09:02", "13:00", "13:30", "18:05"),
        {"first_in": at(DAY, "09:02"), "last_out": at(DAY, "18:05"), "punch_count": 4, "source_raw_ids": (1, 2, 3, 4), "status": "PRESENT"},
    ),
    Case(
        "proc.six_punches_outermost_pair",
        "test_attendance_processing.py::test_six_punches_still_use_the_outermost_pair",
        DAY,
        ("09:00", "11:00", "13:00", "14:00", "16:00", "19:30"),
        {"first_in": at(DAY, "09:00"), "last_out": at(DAY, "19:30"), "punch_count": 6},
    ),
    Case(
        "proc.single_punch_leaves_out_unset",
        "test_attendance_processing.py::test_single_punch_leaves_out_unset",
        DAY,
        ("09:15",),
        {"first_in": at(DAY, "09:15"), "last_out": None, "working_minutes": 0, "status": "LATE"},
    ),
    Case(
        "proc.out_of_order_sorted",
        "test_attendance_processing.py::test_punches_arriving_out_of_order_are_sorted",
        DAY,
        ("18:00", "09:05", "12:00"),
        {"first_in": at(DAY, "09:05"), "last_out": at(DAY, "18:00"), "source_raw_ids": (2, 3, 1)},
    ),
    Case(
        "proc.duplicate_punches",
        "test_attendance_processing.py::test_duplicate_punches_are_all_preserved_in_raw",
        DAY,
        ("09:00", "09:00", "18:00"),
        # The duplicate stays in raw history (ignored_raw_ids) but is not a punch of the day.
        {"punch_count": 2, "first_in": at(DAY, "09:00"), "last_out": at(DAY, "18:00"), "source_raw_ids": (1, 3), "ignored_raw_ids": (2,)},
        v3={"punch_count": 3, "source_raw_ids": (1, 2, 3)},  # v3: 3 punches, ids 1-3 (A3: no debounce)
        changed_by=("A3",),
    ),
    Case("proc.no_punch_absent", "test_attendance_processing.py::test_no_punch_on_a_working_day_is_absent", DAY, (), {"status": "ABSENT"}),
    Case(
        "proc.full_day_present",
        "test_attendance_processing.py::test_full_day_is_present",
        DAY,
        ("09:00", "18:00"),
        {"status": "PRESENT", "working_minutes": 480, "is_late": False},
    ),
    Case(
        "proc.late_after_grace",
        "test_attendance_processing.py::test_arriving_after_grace_is_late",
        DAY,
        ("09:41", "19:00"),
        {"is_late": True, "late_minutes": 31, "status": "LATE"},
    ),
    Case(
        "proc.early_exit",
        "test_attendance_processing.py::test_leaving_before_shift_end_is_early_exit",
        DAY,
        ("09:00", "16:00"),
        {"is_early_exit": True, "early_exit_minutes": 120},
    ),
    Case(
        "proc.short_day_on_time_not_half",
        "test_attendance_processing.py::test_a_short_day_started_on_time_is_not_a_half_day",
        DAY,
        ("09:00", "14:00"),
        {"status": "PRESENT", "working_minutes": 240},
    ),
    Case(
        "proc.arrival_on_v3_deadline",
        "test_attendance_processing.py::test_arriving_exactly_on_the_deadline_is_not_a_half_day",
        DAY,
        ("10:00", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        v3={"status": "LATE"},  # v3: LATE — the 10:00 wall-clock deadline was inclusive (A5: deadline is 09:00 + 30)
        changed_by=("A5",),
    ),
    Case(
        "proc.arrival_after_deadline",
        "test_attendance_processing.py::test_arriving_after_the_deadline_is_a_half_day",
        DAY,
        ("10:01", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240, "break_minutes": 59},
        v3={"working_minutes": 239, "break_minutes": 60},  # v3: 239 worked, full 60-minute break (A2)
        changed_by=("A2",),
    ),
    Case(
        "proc.late_arrival_full_day_not_half",
        "test_attendance_processing.py::test_a_late_arrival_that_still_works_a_full_day_is_not_a_half_day",
        DAY,
        ("11:00", "21:00"),
        {"status": "LATE", "is_late": True, "working_minutes": 540},
    ),
    Case(
        "proc.single_punch_never_half",
        "test_attendance_processing.py::test_a_single_punch_is_never_a_half_day",
        DAY,
        ("09:00",),
        {"status": "PRESENT", "first_in": at(DAY, "09:00"), "last_out": None},
    ),
    Case("proc.weekly_off_not_absent", "test_attendance_processing.py::test_weekly_off_is_not_absent", SUNDAY, (), {"status": "WEEKLY_OFF"}),
    Case("proc.holiday_outranks_absence", "test_attendance_processing.py::test_holiday_outranks_absence", DAY, (), {"status": "HOLIDAY"}, context=HOLIDAY),
    Case(
        "proc.leave_outranks_holiday",
        "test_attendance_processing.py::test_approved_leave_outranks_holiday_and_absence",
        DAY,
        (),
        {"status": "ON_LEAVE"},
        context={**HOLIDAY, **LEAVE},
    ),
    Case(
        "proc.overtime_threshold",
        "test_attendance_processing.py::test_overtime_uses_the_configured_threshold",
        DAY,
        ("08:00", "21:00"),
        {"working_minutes": 720, "overtime_minutes": 240},
    ),
    # --- test_half_day_and_in_out.py --------------------------------------------------------------------------
    Case("half.deadline_0900", "test_half_day_and_in_out.py::test_half_day_follows_the_arrival_deadline[9-0-False]", DAY, ("09:00", "15:00"), {"status": "PRESENT"}),
    Case(
        "half.deadline_0959",
        "test_half_day_and_in_out.py::test_half_day_follows_the_arrival_deadline[9-59-False]",
        DAY,
        ("09:59", "15:00"),
        {"status": "HALF_DAY"},
        v3={"status": "LATE"},  # v3: LATE, not a half day before 10:00 (A5: the 09:00 shift's deadline is 09:30)
        changed_by=("A5",),
    ),
    Case(
        "half.deadline_1000",
        "test_half_day_and_in_out.py::test_half_day_follows_the_arrival_deadline[10-0-False]",
        DAY,
        ("10:00", "15:00"),
        {"status": "HALF_DAY"},
        v3={"status": "LATE"},  # v3: LATE (A5)
        changed_by=("A5",),
    ),
    Case(
        "half.deadline_1001",
        "test_half_day_and_in_out.py::test_half_day_follows_the_arrival_deadline[10-1-True]",
        DAY,
        ("10:01", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        v3={"working_minutes": 239, "break_minutes": 60},  # v3: 239 worked, 60 break (A2)
        changed_by=("A2",),
    ),
    Case(
        "half.deadline_1130",
        "test_half_day_and_in_out.py::test_half_day_follows_the_arrival_deadline[11-30-True]",
        DAY,
        ("11:30", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 210, "break_minutes": 0},
        v3={"working_minutes": 150, "break_minutes": 60},  # v3: a 210-minute span lost its whole break (A2)
        changed_by=("A2",),
    ),
    Case(
        "half.deadline_is_shift_relative",
        "test_half_day_and_in_out.py::test_the_deadline_is_a_clock_time_not_an_allowance",
        DAY,
        ("09:30", "14:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        shift={**GEN, "start_time": time(8, 0)},
        v3={"status": "LATE", "working_minutes": 210, "break_minutes": 60},  # v3: LATE (10:00 deadline, A5); 210 worked, 60 break (A2)
        changed_by=("A2", "A5"),
    ),
    Case(
        "half.allowance_rule",
        "test_half_day_and_in_out.py::test_an_allowance_rule_restores_the_shift_relative_deadline",
        DAY,
        ("09:30", "14:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        shift={**GEN, "start_time": time(8, 0)},
        rules=({"scope": "office", "rules": {"half_day_after_minutes": 60}},),
        v3={"working_minutes": 210, "break_minutes": 60},  # v3: 210 worked, 60 break (A2)
        changed_by=("A2",),
    ),
    Case(
        "half.clock_rule_overrides",
        "test_half_day_and_in_out.py::test_an_attendance_rule_overrides_the_deadline",
        DAY,
        ("11:00", "15:00"),
        {"status": "LATE", "working_minutes": 240},
        rules=({"scope": "office", "rules": {"half_day_after": "11:30"}},),
        v3={"working_minutes": 180, "break_minutes": 60},  # v3: 180 worked, 60 break (A2)
        changed_by=("A2",),
    ),
    Case(
        "half.shift_rule_beats_office_rule",
        "test_half_day_and_in_out.py::test_a_shift_rule_beats_an_office_rule",
        DAY,
        ("10:00", "15:00"),
        {"status": "HALF_DAY"},
        rules=({"scope": "office", "rules": {"half_day_after": "11:30"}}, {"scope": "shift", "rules": {"half_day_after": "09:30"}}),
    ),
    Case(
        "half.short_hours_alone_not_half",
        "test_half_day_and_in_out.py::test_short_hours_alone_never_make_a_half_day",
        DAY,
        ("09:00", "11:00"),
        {"status": "PRESENT", "working_minutes": 120},
        rules=({"scope": "office", "rules": {"half_day_under_minutes": 240}},),
        v3={"working_minutes": 60, "break_minutes": 60},  # v3: 60 (A2)
        changed_by=("A2",),
    ),
    Case(
        "half.weekly_off_no_deadline",
        "test_half_day_and_in_out.py::test_a_holiday_or_weekly_off_has_no_deadline_to_miss",
        SUNDAY,
        ("11:00", "15:00"),
        {"status": "WEEKLY_OFF", "worked_on_off_day": True, "working_minutes": 240, "overtime_minutes": 240, "is_late": False, "late_minutes": 0},
        # v3: PRESENT with 180 worked, late 110 and an early exit of 180 on a day nobody was scheduled (A4, A2)
        v3={
            "status": "PRESENT",
            "working_minutes": 180,
            "break_minutes": 60,
            "overtime_minutes": 0,
            "late_minutes": 110,
            "is_late": True,
            "early_exit_minutes": 180,
            "is_early_exit": True,
        },
        changed_by=("A2", "A4"),
    ),
    Case(
        "half.real_in_and_out",
        "test_half_day_and_in_out.py::test_in_and_out_are_the_real_first_and_last_punch",
        DAY,
        ("09:03", "13:15", "14:05", "18:22"),
        {"first_in": at(DAY, "09:03"), "last_out": at(DAY, "18:22"), "missing_out": False},
    ),
    Case(
        "half.missing_out_named",
        "test_half_day_and_in_out.py::test_a_missing_out_is_named_and_never_invented",
        DAY,
        ("09:03",),
        {"first_in": at(DAY, "09:03"), "last_out": None, "missing_out": True},
    ),
    Case(
        "half.absent_no_missing_out",
        "test_half_day_and_in_out.py::test_a_day_nobody_attended_has_no_out_and_no_missing_out",
        DAY,
        (),
        {"status": "ABSENT", "last_out": None, "missing_out": False},
    ),
    Case("half.agree_full_day", "test_half_day_and_in_out.py::test_daily_calendar_and_monthly_agree_on_the_same_day[in_at0-out_at0]", DAY, ("09:00", "18:05"), {"status": "PRESENT"}),
    Case(
        "half.agree_half_by_arrival",
        "test_half_day_and_in_out.py::test_daily_calendar_and_monthly_agree_on_the_same_day[in_at1-out_at1]",
        DAY,
        ("10:30", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        v3={"working_minutes": 210, "break_minutes": 60},  # v3: 210 worked, 60 break (A2)
        changed_by=("A2",),
    ),
    Case("half.agree_short_on_time", "test_half_day_and_in_out.py::test_daily_calendar_and_monthly_agree_on_the_same_day[in_at2-out_at2]", DAY, ("09:00", "14:00"), {"status": "PRESENT"}),
    Case("half.agree_late_full", "test_half_day_and_in_out.py::test_daily_calendar_and_monthly_agree_on_the_same_day[in_at3-out_at3]", DAY, ("09:41", "19:00"), {"status": "LATE"}),
    # --- test_employee_list_active_default.py -----------------------------------------------------------------
    Case(
        "cutoff.production_shift_1015",
        "test_employee_list_active_default.py::test_the_cut_off_is_ten_whatever_time_the_shift_starts",
        DAY,
        ("10:15", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        shift={**GEN, "start_time": time(9, 30)},
        v3={"working_minutes": 225, "break_minutes": 60},  # v3: 225 worked, 60 break (A2)
        changed_by=("A2",),
    ),
    *(
        Case(
            f"cutoff.to_the_second_{clock.replace(':', '')}",
            f"test_employee_list_active_default.py::test_the_cut_off_is_exact_to_the_second[{clock}]",
            DAY,
            (clock, "15:00"),
            {"status": status, "working_minutes": working},
            shift={**GEN, "start_time": time(9, 30)},
            v3=v3,
            changed_by=("A2",) if v3 else (),
        )
        for clock, status, working, v3 in (
            ("09:00:00", "PRESENT", 300, {}),
            ("09:30:00", "PRESENT", 270, {}),
            ("09:59:59", "LATE", 240, {}),
            ("10:00:00", "LATE", 240, {}),  # the 10:00 deadline of a 09:30 shift is the same in v3 and v4
            ("10:00:01", "HALF_DAY", 240, {"working_minutes": 239, "break_minutes": 60}),  # v3: 239 worked (A2)
            ("10:30:00", "HALF_DAY", 240, {"working_minutes": 210, "break_minutes": 60}),  # v3: 210 worked (A2)
            ("11:00:00", "HALF_DAY", 240, {"working_minutes": 180, "break_minutes": 60}),  # v3: 180 worked (A2)
        )
    ),
    Case(
        "cutoff.half_day_counts_present",
        "test_employee_list_active_default.py::test_a_half_day_is_counted_as_a_day_present",
        DAY,
        ("10:30", "18:00"),
        {"status": "HALF_DAY", "working_minutes": 390},
        shift={**GEN, "start_time": time(9, 30)},
    ),
    Case(
        "cutoff.rule_shift_relative_deadline",
        "test_employee_list_active_default.py::test_a_rule_can_still_ask_for_a_shift_relative_deadline",
        DAY,
        ("09:30", "15:00"),
        {"status": "HALF_DAY"},
        shift={**GEN, "start_time": time(8, 0)},
        rules=({"scope": "office", "rules": {"half_day_after_minutes": 60}},),
    ),
    Case(
        "cutoff.monthly_in_and_out",
        "test_employee_list_active_default.py::test_the_monthly_report_never_hides_in_and_out",
        DAY,
        ("09:02", "18:10"),
        {"first_in": at(DAY, "09:02"), "last_out": at(DAY, "18:10"), "punch_count": 2},
    ),
    # --- test_multi_device_attendance.py ----------------------------------------------------------------------
    Case(
        "multi.two_devices_one_day",
        "test_multi_device_attendance.py::test_first_punch_device1_last_punch_device2",
        DAY,
        (("09:02", "MARS-01"), ("18:05", "MARS-02")),
        {"first_in": at(DAY, "09:02"), "last_out": at(DAY, "18:05"), "punch_count": 2, "status": "PRESENT", "first_device": "MARS-01"},
    ),
    Case(
        "multi.intermediate_across_devices",
        "test_multi_device_attendance.py::test_intermediate_punches_across_devices_make_no_extra_session",
        DAY,
        (("09:00", "MARS-01"), ("11:30", "MARS-02"), ("13:00", "MARS-01"), ("14:00", "MARS-02"), ("18:30", "MARS-01")),
        {"first_in": at(DAY, "09:00"), "last_out": at(DAY, "18:30"), "punch_count": 5, "source_raw_ids": (1, 2, 3, 4, 5)},
    ),
    Case(
        "multi.single_punch",
        "test_multi_device_attendance.py::test_single_punch_gives_in_and_no_invented_out",
        DAY,
        (("09:05", "MARS-01"),),
        {"first_in": at(DAY, "09:05"), "last_out": None, "punch_count": 1, "working_minutes": 0},
    ),
    Case("multi.no_punch_absent", "test_multi_device_attendance.py::test_no_punch_on_a_working_day_is_absent", DAY, (), {"status": "ABSENT", "first_in": None, "last_out": None}),
    Case("multi.approved_leave", "test_multi_device_attendance.py::test_approved_leave_shows_as_leave", DAY, (), {"status": "ON_LEAVE"}, context=LEAVE),
    Case("multi.holiday_survives", "test_multi_device_attendance.py::test_holiday_and_weekend_survive_recalculation", DAY, (), {"status": "HOLIDAY"}, context=HOLIDAY),
    Case("multi.weekend_survives", "test_multi_device_attendance.py::test_holiday_and_weekend_survive_recalculation", SUNDAY, (), {"status": "WEEKLY_OFF"}),
    Case(
        "multi.same_second_two_devices",
        "test_multi_device_attendance.py::test_same_punch_from_two_devices_is_two_punches",
        DAY,
        (("09:03", "MARS-01"), ("09:03", "MARS-02")),
        # Two raw punches (two devices, two dedup keys) — one of them is the day's IN, the other a double scan.
        {"punch_count": 1, "last_out": None, "ignored_raw_ids": (2,), "status": "PRESENT", "is_early_exit": False},
        # v3: IN and OUT at the same second — 0 minutes, and a 537-minute "early exit" (A3)
        v3={"punch_count": 2, "last_out": at(DAY, "09:03"), "is_early_exit": True, "early_exit_minutes": 537, "source_raw_ids": (1, 2)},
        changed_by=("A3",),
    ),
    # --- scripts/attendance_logic_test.py (seed shift 09:30–18:30, Monday 2026-08-17) ------------------------
    Case(
        "logic.first_in_last_out",
        "attendance_logic_test.py::first punch is IN and last punch is OUT",
        WORKDAY,
        ("09:20", "12:30", "13:30", "18:15"),
        {"first_in": at(WORKDAY, "09:20"), "last_out": at(WORKDAY, "18:15")},
        shift=SEED,
    ),
    Case(
        "logic.intermediate_counted",
        "attendance_logic_test.py::intermediate punches are counted, not discarded",
        WORKDAY,
        ("09:15", "12:30", "13:30", "18:10"),
        {"punch_count": 4, "source_raw_ids": (1, 2, 3, 4)},
        shift=SEED,
    ),
    Case("logic.single_punch", "attendance_logic_test.py::a single punch leaves OUT unset", WORKDAY, ("09:20",), {"last_out": None, "working_minutes": 0}, shift=SEED),
    Case("logic.absent", "attendance_logic_test.py::no punch on a scheduled working day is ABSENT", WORKDAY, (), {"status": "ABSENT"}, shift=SEED),
    Case("logic.weekly_off", "attendance_logic_test.py::no punch on a weekly off is WEEKLY_OFF, not ABSENT", LOGIC_SUNDAY, (), {"status": "WEEKLY_OFF"}, shift=SEED),
    Case("logic.holiday", "attendance_logic_test.py::a holiday outranks absence", WORKDAY, (), {"status": "HOLIDAY"}, shift=SEED, context=HOLIDAY),
    Case("logic.leave", "attendance_logic_test.py::approved leave outranks a holiday and absence", WORKDAY, (), {"status": "ON_LEAVE"}, shift=SEED, context={**HOLIDAY, **LEAVE}),
    Case("logic.0925_on_time", "attendance_logic_test.py::09:25 is on time", WORKDAY, ("09:25", "18:30"), {"is_late": False, "status": "PRESENT"}, shift=SEED),
    Case(
        "logic.0935_within_grace",
        "attendance_logic_test.py::09:35 is within grace and still on time",
        WORKDAY,
        ("09:35", "18:30"),
        {"is_late": False, "status": "PRESENT", "working_minutes": 475},
        shift=SEED,
    ),
    Case("logic.0941_late", "attendance_logic_test.py::09:41 is past grace and is LATE", WORKDAY, ("09:41", "18:41"), {"is_late": True, "late_minutes": 1, "status": "LATE"}, shift=SEED),
    Case(
        "logic.late_threshold",
        "attendance_logic_test.py::the late threshold is configurable and delays the flag",
        WORKDAY,
        ("09:41", "18:41"),
        {"is_late": False},
        shift={**SEED, "late_threshold_minutes": 20},
    ),
    Case(
        "logic.grace_zero",
        "attendance_logic_test.py::the grace period is configurable",
        WORKDAY,
        ("09:35", "18:35"),
        {"is_late": True, "late_minutes": 5},
        shift={**SEED, "grace_minutes": 0},
    ),
    Case(
        "logic.break_deducted",
        "attendance_logic_test.py::working time is OUT minus IN, less the configured break",
        WORKDAY,
        ("09:30", "18:30"),
        {"working_minutes": 480, "break_minutes": 60},
        shift=SEED,
    ),
    Case(
        "logic.short_span_keeps_break",
        "attendance_logic_test.py::a short span keeps its break rather than going to zero",
        WORKDAY,
        ("09:30", "10:00"),
        {"working_minutes": 30, "break_minutes": 0},
        shift=SEED,
    ),
    Case(
        "logic.break_off",
        "attendance_logic_test.py::break deduction can be switched off",
        WORKDAY,
        ("09:30", "18:30"),
        {"working_minutes": 540},
        shift={**SEED, "auto_deduct_break": False},
    ),
    Case(
        "logic.never_negative_same_instant",
        "attendance_logic_test.py::working time is never negative",
        WORKDAY,
        ("09:30", "09:30"),
        {"working_minutes": 0, "punch_count": 1, "last_out": None},
        shift=SEED,
        v3={"punch_count": 2, "last_out": at(WORKDAY, "09:30"), "source_raw_ids": (1, 2), "is_early_exit": True, "early_exit_minutes": 540},  # v3: IN = OUT (A3)
        changed_by=("A3",),
    ),
    Case(
        "logic.never_negative_long_break",
        "attendance_logic_test.py::working time is never negative",
        WORKDAY,
        ("09:30", "18:30"),
        {"working_minutes": 240, "break_minutes": 300},
        shift={**SEED, "break_minutes": 600},
        v3={"working_minutes": 540, "break_minutes": 0, "overtime_minutes": 60},  # v3: a 540-minute span kept its 600-minute break undeducted (A2)
        changed_by=("A2",),
    ),
    Case(
        "logic.never_negative_short_long_break",
        "attendance_logic_test.py::working time is never negative",
        WORKDAY,
        ("09:30", "10:00"),
        {"working_minutes": 30, "break_minutes": 0},
        shift={**SEED, "break_minutes": 600},
    ),
    Case(
        "logic.stale_half_day_script",
        "attendance_logic_test.py::half day is applied between the configured thresholds (stale: expects HALF_DAY; v3 and v4 say PRESENT)",
        WORKDAY,
        ("09:30", "14:30"),
        {"working_minutes": 240, "status": "PRESENT"},
        shift=SEED,
    ),
    Case(
        "logic.overtime",
        "attendance_logic_test.py::overtime starts after the configured point",
        WORKDAY,
        ("09:30", "20:30"),
        {"working_minutes": 600, "overtime_minutes": 120},
        shift=SEED,
    ),
    Case(
        "logic.overnight_one_day",
        "attendance_logic_test.py::an overnight shift produces one work day, not two halves",
        WORKDAY,
        (at(WORKDAY, "22:00"), at(WORKDAY + timedelta(days=1), "06:00")),
        {"working_minutes": 420, "status": "PRESENT"},
        shift=NIGHT,
    ),
    Case(
        "logic.codes_do_not_decide_direction",
        "attendance_logic_test.py::device status and punch codes are not used to decide direction",
        WORKDAY,
        ("09:20", "18:15"),
        {"first_in": at(WORKDAY, "09:20"), "last_out": at(WORKDAY, "18:15")},
        shift=SEED,
    ),
    Case(
        "logic.out_of_order",
        "attendance_logic_test.py::punches out of order are sorted before IN and OUT are chosen",
        WORKDAY,
        ("18:15", "09:20", "12:30"),
        {"first_in": at(WORKDAY, "09:20"), "last_out": at(WORKDAY, "18:15")},
        shift=SEED,
    ),
    # --- eSSL spec §C5 worked examples (seed shift 09:30–18:30) -----------------------------------------------
    Case("worked.0935_1830", "spec §C5", DAY, ("09:35", "18:30"), {"working_minutes": 475, "status": "PRESENT", "is_late": False}, shift=SEED),
    Case("worked.0945_1830", "spec §C5", DAY, ("09:45", "18:30"), {"late_minutes": 5, "is_late": True, "working_minutes": 465, "status": "LATE"}, shift=SEED),
    Case("worked.1015_1830", "spec §C5", DAY, ("10:15", "18:30"), {"working_minutes": 435, "status": "HALF_DAY"}, shift=SEED),
    Case("worked.1015_1930", "spec §C5", DAY, ("10:15", "19:30"), {"working_minutes": 495, "status": "LATE"}, shift=SEED),
    Case("worked.0930_2030", "spec §C5", DAY, ("09:30", "20:30"), {"working_minutes": 600, "overtime_minutes": 120, "status": "PRESENT"}, shift=SEED),
    Case("worked.1030_only", "spec §C5", DAY, ("10:30",), {"status": "HALF_DAY", "last_out": None, "missing_out": True}, shift=SEED),
    Case("worked.0930_only", "spec §C5", DAY, ("09:30",), {"status": "PRESENT", "last_out": None, "missing_out": True}, shift=SEED),
    # --- explicit v4 cases -----------------------------------------------------------------------------------
    Case(
        "v4.a3_double_scan",
        "A3",
        DAY,
        ("09:00:00", "09:00:30"),
        {"punch_count": 1, "last_out": None, "missing_out": True, "working_minutes": 0, "ignored_raw_ids": (2,), "status": "PRESENT", "is_early_exit": False},
        v3={"punch_count": 2, "last_out": at(DAY, "09:00:30"), "source_raw_ids": (1, 2), "is_early_exit": True, "early_exit_minutes": 539},  # v3: IN+OUT, 0 minutes (A3)
        changed_by=("A3",),
    ),
    Case(
        "v4.a3_debounce_from_last_accepted",
        "A3",
        DAY,
        ("09:00:00", "09:01:30", "09:02:30", "18:00:00"),
        {"punch_count": 3, "source_raw_ids": (1, 3, 4), "ignored_raw_ids": (2,)},
        v3={"punch_count": 4, "source_raw_ids": (1, 2, 3, 4)},  # v3: every scan counted (A3)
        changed_by=("A3",),
    ),
    Case("v4.a3_exactly_debounce_apart", "A3", DAY, ("09:00", "09:02", "18:00"), {"punch_count": 3, "ignored_raw_ids": ()}),
    Case(
        "v4.a3_debounce_disabled",
        "A3",
        DAY,
        ("09:00:00", "09:00:30", "18:00:00"),
        {"punch_count": 3, "ignored_raw_ids": ()},
        shift={**GEN, "debounce_minutes": 0},
    ),
    Case(
        "v4.a4_worked_holiday",
        "A4",
        DAY,
        ("09:45", "18:30"),
        {"status": "HOLIDAY", "worked_on_off_day": True, "working_minutes": 465, "overtime_minutes": 465, "is_late": False, "late_minutes": 0},
        context=HOLIDAY,
        v3={"status": "LATE", "overtime_minutes": 0, "is_late": True, "late_minutes": 35},  # v3: a worked holiday read LATE (A4)
        changed_by=("A4",),
    ),
    Case(
        "v4.a4_worked_weekly_off",
        "A4",
        SUNDAY,
        ("09:00", "18:00"),
        {"status": "WEEKLY_OFF", "worked_on_off_day": True, "working_minutes": 480, "overtime_minutes": 480},
        v3={"status": "PRESENT", "overtime_minutes": 0},  # v3: PRESENT, no overtime (A4)
        changed_by=("A4",),
    ),
    Case(
        "v4.a4_worked_weekly_off_overtime_disabled",
        "A4",
        SUNDAY,
        ("09:00", "18:00"),
        {"status": "WEEKLY_OFF", "worked_on_off_day": True, "overtime_minutes": 0},
        shift={**GEN, "overtime_enabled": False},
        v3={"status": "PRESENT"},  # v3: PRESENT (A4)
        changed_by=("A4",),
    ),
    Case(
        "v4.a4_leave_with_punches",
        "A4",
        DAY,
        ("09:00", "18:00"),
        {"status": "PRESENT", "leave_conflict": True, "worked_on_off_day": False},
        context=LEAVE,
        changed_by=("A4",),  # v3: PRESENT as well, but the conflict with the approved leave went unnoticed
    ),
    Case(
        "v4.a4_holiday_leave_with_punches",
        "A4",
        DAY,
        ("09:00", "18:00"),
        {"status": "HOLIDAY", "leave_conflict": True, "worked_on_off_day": True, "overtime_minutes": 480},
        context={**HOLIDAY, **LEAVE},
        v3={"status": "PRESENT", "overtime_minutes": 0},  # v3: PRESENT (A4)
        changed_by=("A4",),
    ),
    Case(
        "v4.a4_half_day_leave_with_punches",
        "A4",
        DAY,
        ("09:00", "13:00"),
        {"status": "HALF_DAY", "leave_conflict": False, "working_minutes": 240},
        context=HALF_LEAVE,
        v3={"working_minutes": 180, "break_minutes": 60},  # v3: 180 worked (A2); the status is unchanged
        changed_by=("A2",),
    ),
    Case("v4.a4_half_day_leave_no_punches", "A4", DAY, (), {"status": "ON_LEAVE"}, context=HALF_LEAVE),
    Case(
        "v4.a5_night_shift_deadline",
        "A5",
        DAY,
        (at(DAY, "22:45"), at(NEXT_DAY, "06:00")),
        {"status": "HALF_DAY", "working_minutes": 375},
        shift=NIGHT,
        v3={"status": "LATE"},  # v3: the default deadline rolled to 10:00 the next morning — never a half day (A5)
        changed_by=("A5",),
    ),
    Case(
        "v4.a5_specific_rule_in_minutes_beats_office_clock",
        "A5",
        DAY,
        ("10:30", "15:00"),
        {"status": "HALF_DAY", "working_minutes": 240},
        rules=({"scope": "office", "rules": {"half_day_after": "11:30"}}, {"scope": "shift", "rules": {"half_day_after_minutes": 60}}),
        v3={"status": "LATE", "working_minutes": 210, "break_minutes": 60},  # v3: the office clock 11:30 won over the shift rule (A5); 210 (A2)
        changed_by=("A2", "A5"),
    ),
    Case(
        "v4.a6_overnight_with_buffer",
        "A6",
        DAY,
        (at(DAY, "22:00"), at(NEXT_DAY, "06:00"), at(NEXT_DAY, "07:30")),
        {"last_out": at(NEXT_DAY, "07:30"), "working_minutes": 510, "overtime_minutes": 30, "punch_count": 3},
        shift=NIGHT,
    ),
    Case(
        "v4.no_shift_no_deadline",
        "A5",
        DAY,
        ("10:30", "15:00"),
        {"status": "PRESENT", "working_minutes": 270, "late_minutes": 0},
        shift=None,
        v3={"status": "HALF_DAY"},  # v3: 10:00 wall-clock deadline even without a shift (A5)
        changed_by=("A5",),
    ),
    Case("v4.no_shift_sunday_off", "C3", SUNDAY, (), {"status": "WEEKLY_OFF"}, shift=None),
    Case(
        "v4.no_shift_rule_clock_deadline",
        "A5",
        DAY,
        ("11:30", "15:00"),
        {"status": "HALF_DAY"},
        shift=None,
        rules=({"scope": "global", "rules": {"half_day_after": "11:00"}},),
    ),
)

# --- half_day_deadline ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeadlineCase:
    id: str
    day: date
    shift: dict | None
    rules: tuple
    expect: datetime | None
    v3: Any = "same"
    changed_by: tuple = ()


DEADLINES: tuple[DeadlineCase, ...] = (
    DeadlineCase("gen_default", DAY, GEN, (), at(DAY, "09:30"), at(DAY, "10:00"), ("A5",)),  # v3: 10:00 wall clock
    DeadlineCase("seed_default", DAY, SEED, (), at(DAY, "10:00")),
    DeadlineCase("early_start", DAY, {**GEN, "start_time": time(8, 0)}, (), at(DAY, "08:30"), at(DAY, "10:00"), ("A5",)),  # v3: 10:00
    DeadlineCase("office_minutes_rule", DAY, {**GEN, "start_time": time(8, 0)}, ({"scope": "office", "rules": {"half_day_after_minutes": 60}},), at(DAY, "09:00")),
    DeadlineCase("office_clock_rule", DAY, GEN, ({"scope": "office", "rules": {"half_day_after": "11:30"}},), at(DAY, "11:30")),
    DeadlineCase("global_clock_rule_with_seconds", DAY, GEN, ({"scope": "global", "rules": {"half_day_after": "10:15:30"}},), at(DAY, "10:15:30")),
    DeadlineCase("night_default", DAY, NIGHT, (), at(DAY, "22:30"), at(NEXT_DAY, "10:00"), ("A5",)),  # v3: 10:00 next morning
    DeadlineCase("night_clock_rule_rolls", DAY, NIGHT, ({"scope": "shift", "rules": {"half_day_after": "02:00"}},), at(NEXT_DAY, "02:00")),
    DeadlineCase("no_shift", DAY, None, (), None, at(DAY, "10:00"), ("A5",)),  # v3: 10:00 without any shift
    DeadlineCase("no_shift_clock_rule", DAY, None, ({"scope": "global", "rules": {"half_day_after": "11:00"}},), at(DAY, "11:00")),
    DeadlineCase(
        "shift_minutes_beat_office_clock",
        DAY,
        GEN,
        ({"scope": "office", "rules": {"half_day_after": "11:30"}}, {"scope": "shift", "rules": {"half_day_after_minutes": 60}}),
        at(DAY, "10:00"),
        at(DAY, "11:30"),  # v3: the clock time won regardless of scope
        ("A5",),
    ),
    DeadlineCase(
        "future_rule_ignored",
        DAY,
        SEED,
        ({"scope": "shift", "rules": {"half_day_after": "12:00"}, "effective_from": DAY + timedelta(days=1)},),
        at(DAY, "10:00"),
    ),
    DeadlineCase(
        "latest_effective_rule_wins",
        DAY,
        SEED,
        (
            {"scope": "office", "rules": {"half_day_after": "11:00"}, "effective_from": date(2026, 1, 1)},
            {"scope": "office", "rules": {"half_day_after": "10:30"}, "effective_from": date(2026, 6, 1)},
        ),
        at(DAY, "10:30"),
    ),
    DeadlineCase(
        "same_rule_clock_wins_over_minutes",
        DAY,
        SEED,
        ({"scope": "office", "rules": {"half_day_after": "11:00", "half_day_after_minutes": 15}},),
        at(DAY, "11:00"),
    ),
    DeadlineCase("malformed_rule_ignored", DAY, SEED, ({"scope": "office", "rules": {"half_day_after": "late", "half_day_after_minutes": -5}},), at(DAY, "10:00")),
)

# --- attribute_work_date ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributionCase:
    id: str
    shift: dict | None
    wall_clock: datetime
    expect: date
    v3: Any = "same"
    changed_by: tuple = ()


CLAMPED_NIGHT = {"start_time": time(18, 0), "end_time": time(6, 0), "is_overnight": True, "overnight_buffer_minutes": 720}

ATTRIBUTIONS: tuple[AttributionCase, ...] = (
    AttributionCase("night_start", NIGHT, at(DAY, "22:00"), DAY),
    AttributionCase("night_end", NIGHT, at(NEXT_DAY, "06:00"), DAY),
    AttributionCase("night_after_end_in_buffer", NIGHT, at(NEXT_DAY, "06:30"), DAY, NEXT_DAY, ("A6",)),  # v3: a new single-punch day
    AttributionCase("night_buffer_edge", NIGHT, at(NEXT_DAY, "09:00"), DAY, NEXT_DAY, ("A6",)),  # v3: next day
    AttributionCase("night_after_buffer", NIGHT, at(NEXT_DAY, "09:00:01"), NEXT_DAY),
    AttributionCase("night_evening_before_start", NIGHT, at(DAY, "21:59"), DAY),
    AttributionCase("day_shift_after_midnight", GEN, at(DAY, "00:30"), DAY),
    AttributionCase("no_shift", None, at(DAY, "03:00"), DAY),
    AttributionCase("buffer_clamped_below_start", CLAMPED_NIGHT, at(NEXT_DAY, "17:59"), DAY, NEXT_DAY, ("A6",)),  # v3: next day
    AttributionCase("buffer_clamped_at_start", CLAMPED_NIGHT, at(NEXT_DAY, "18:00"), NEXT_DAY),
)

# --- break deduction (A2) -----------------------------------------------------------------------------------------

#: Gross minutes whose working minutes v4 changes for the fixture shift (break 60, half day 240): v3 deducted the
#: whole break from any span longer than it; v4 deducts nothing up to the half day and ramps to the full break at
#: half day + break.
BREAK_CHANGED_GROSS = frozenset(range(61, 300))
BREAK_GROSS_RANGE = range(0, 601)
