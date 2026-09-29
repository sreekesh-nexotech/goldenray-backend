"""engines.attendance.recompute — v3 ``process_range`` without a database: identity (A1), attribution (A6), the
future-date guard (A7), affected dates (A8), office timezone (A10), corrections (A12), and the eSSL process_range
cases (unmapped PIN, pending leave, join date, manual override, multi-device days, idempotency)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from engines import attendance as att
from engines.tests import attendance_cases as cases
from engines.tests.attendance_support import aware, shift

DAY = cases.DAY
SUNDAY = cases.SUNDAY
#: "now" well after the test month, so every September day is in the past unless a test says otherwise.
LATER = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)

GEN_SHIFT = shift(cases.GEN)
AROMAL = att.Employee(key="EMP001", office="MAIN", shift=GEN_SHIFT, joined_on=date(2026, 1, 1))
BINDU = att.Employee(key="EMP002", office="MAIN", shift=GEN_SHIFT, joined_on=date(2026, 1, 1))
LINKS = [att.DeviceLink("MARS-01", "1", "EMP001"), att.DeviceLink("MARS-02", "1", "EMP001")]


def raw(raw_id, device, pin, day, clock, timezone=cases.TIMEZONE):
    return att.RawPunch(raw_id=raw_id, device=device, pin=pin, punch_at=aware(cases.at(day, clock), timezone))


def run(punches=(), employees=(AROMAL,), date_from=DAY, date_to=DAY, links=LINKS, now=LATER, **kwargs):
    return att.recompute(date_from=date_from, date_to=date_to, employees=list(employees), punches=punches, links=links, now=now, **kwargs)


def day_of(result, employee="EMP001", day=DAY):
    found = [write.result for write in result.writes if write.employee == employee and write.result.work_date == day]
    assert len(found) <= 1
    return found[0] if found else None


# ---------------------------------------------------------------------------------------------------------------
# eSSL process_range cases
# ---------------------------------------------------------------------------------------------------------------


def test_two_devices_one_employee_day_produce_one_row():
    result = run([raw(1, "MARS-01", "1", DAY, "09:02"), raw(2, "MARS-02", "1", DAY, "18:05")])
    days = [write for write in result.writes if write.result.work_date == DAY]
    assert len(days) == 1
    day = days[0].result
    assert (day.first_in, day.last_out, day.punch_count, day.status, day.first_device) == (
        datetime(2026, 9, 1, 9, 2),
        datetime(2026, 9, 1, 18, 5),
        2,
        att.Status.PRESENT,
        "MARS-01",
    )


def test_no_punch_on_a_working_day_is_written_absent():
    result = run()
    assert day_of(result).status == att.Status.ABSENT
    assert result.absent_days == 1


def test_pending_leave_does_not_change_the_day():
    pending = att.Leave(employee="EMP001", date_from=DAY, date_to=DAY, status="PENDING", leave_type="CASUAL")
    assert day_of(run(leaves=[pending])).status == att.Status.ABSENT
    approved = att.Leave(employee="EMP001", date_from=DAY, date_to=DAY, status="APPROVED", leave_type="CASUAL")
    day = day_of(run(leaves=[approved]))
    assert day.status == att.Status.ON_LEAVE


def test_holiday_on_the_posting_office_or_all_offices():
    assert day_of(run(holidays=[att.Holiday(DAY, "Onam", office="MAIN")])).status == att.Status.HOLIDAY
    assert day_of(run(holidays=[att.Holiday(DAY, "Onam")])).status == att.Status.HOLIDAY
    assert day_of(run(holidays=[att.Holiday(DAY, "Branch day", office="BRANCH")])).status == att.Status.ABSENT
    assert day_of(run(holidays=[att.Holiday(DAY, "Onam", is_active=False)])).status == att.Status.ABSENT


def test_leave_outranks_holiday_without_punches():
    leave = att.Leave(employee="EMP001", date_from=DAY - timedelta(days=2), date_to=DAY + timedelta(days=2))
    result = run(holidays=[att.Holiday(DAY, "Onam")], leaves=[leave])
    assert day_of(result).status == att.Status.ON_LEAVE


def test_a_full_day_leave_outranks_a_half_day_leave_on_the_same_date():
    leaves = [att.Leave("EMP001", DAY, DAY, is_half_day=True, leave_type="HALF"), att.Leave("EMP001", DAY, DAY, leave_type="FULL")]
    book = att.LeaveBook(leaves)
    assert book.on("EMP001", DAY).leave_type == "FULL"
    assert att.day_context("EMP001", "MAIN", DAY, att.HolidayCalendar(), book) == att.DayContext(on_leave=True, leave_type="FULL")


def test_nothing_is_recorded_before_the_join_date_or_after_leaving():
    joined_later = att.Employee(key="EMP001", shift=GEN_SHIFT, joined_on=DAY + timedelta(days=1))
    assert run(employees=[joined_later]).writes == ()
    left = att.Employee(key="EMP001", shift=GEN_SHIFT, joined_on=date(2026, 1, 1), left_on=DAY - timedelta(days=1))
    assert run(employees=[left]).writes == ()


def test_recompute_is_deterministic_and_reads_its_inputs_only():
    punches = [raw(1, "MARS-01", "1", DAY, "09:00"), raw(2, "MARS-01", "1", DAY, "18:00")]
    first = run(punches)
    second = run(punches)
    assert first == second
    assert [p.raw_id for p in punches] == [1, 2]


def test_weekly_offs_are_written_too():
    result = run(date_from=DAY, date_to=SUNDAY)
    assert day_of(result, day=SUNDAY).status == att.Status.WEEKLY_OFF  # v3 left quiet weekly offs unstored (A7: finalised)
    assert len(result.writes) == 6


def test_the_window_must_not_run_backwards():
    with pytest.raises(ValueError):
        run(date_from=DAY, date_to=DAY - timedelta(days=1))


# ---------------------------------------------------------------------------------------------------------------
# A1 — identity is (device, pin)
# ---------------------------------------------------------------------------------------------------------------


def test_the_same_pin_on_another_device_is_another_person():
    links = [att.DeviceLink("OFFICE-1", "1", "EMP001"), att.DeviceLink("OFFICE-2", "1", "EMP002")]
    punches = [raw(1, "OFFICE-1", "1", DAY, "09:00"), raw(2, "OFFICE-1", "1", DAY, "18:00"), raw(3, "OFFICE-2", "1", DAY, "10:30"), raw(4, "OFFICE-2", "1", DAY, "15:00")]
    result = run(punches, employees=[AROMAL, BINDU], links=links)
    assert day_of(result, "EMP001").source_raw_ids == (1, 2)
    assert day_of(result, "EMP002").source_raw_ids == (3, 4)  # v3: one global PIN map, the last write won


def test_an_unmapped_pin_is_reported_per_device_and_attributed_to_nobody():
    links = [att.DeviceLink("MARS-01", "1", "EMP001")]
    punches = [raw(1, "MARS-01", "999", DAY, "09:00"), raw(2, "MARS-02", "1", DAY, "09:00"), raw(3, "MARS-01", "1", DAY, "09:10")]
    result = run(punches, links=links)
    assert result.unmapped == {"MARS-01": ("999",), "MARS-02": ("1",)}  # "1" is linked on MARS-01 only
    assert day_of(result).source_raw_ids == (3,)
    assert result.counts()["unmapped_pins"] == 2
    assert result.raw_punches_considered == 3


def test_no_employee_code_fallback():
    result = run([raw(1, "MARS-01", "EMP001", DAY, "09:00")], links=[])
    assert day_of(result).status == att.Status.ABSENT
    assert result.unmapped == {"MARS-01": ("EMP001",)}


def test_one_pin_on_one_device_cannot_belong_to_two_employees():
    with pytest.raises(ValueError):
        att.build_identity_map([att.DeviceLink("D", "1", "A"), att.DeviceLink("D", "1", "B")])
    assert att.build_identity_map([att.DeviceLink("D", " 1 ", "A"), att.DeviceLink("D", "1", "A")]) == {("D", "1"): "A"}


def test_links_may_be_a_prebuilt_identity_map():
    result = run([raw(1, "MARS-01", "1", DAY, "09:00")], links={("MARS-01", "1"): "EMP001"})
    assert day_of(result).first_in == datetime(2026, 9, 1, 9, 0)


def test_punches_of_employees_outside_the_run_are_ignored():
    result = run([raw(1, "MARS-01", "1", DAY, "09:00")], employees=[BINDU])
    assert day_of(result, "EMP002").status == att.Status.ABSENT
    assert result.unmapped == {}


# ---------------------------------------------------------------------------------------------------------------
# A6 — overnight attribution inside a recompute
# ---------------------------------------------------------------------------------------------------------------


def test_overnight_punches_in_the_buffer_belong_to_the_previous_work_date():
    night_worker = att.Employee(key="EMP001", shift=shift(cases.NIGHT), joined_on=date(2026, 1, 1))
    punches = [raw(1, "MARS-01", "1", DAY, "22:00"), raw(2, "MARS-01", "1", cases.NEXT_DAY, "06:30")]
    result = run(punches, employees=[night_worker], date_from=DAY, date_to=cases.NEXT_DAY)
    first = day_of(result, day=DAY)
    assert (first.first_in, first.last_out, first.working_minutes) == (datetime(2026, 9, 1, 22, 0), datetime(2026, 9, 2, 6, 30), 450)
    assert day_of(result, day=cases.NEXT_DAY).status == att.Status.ABSENT  # v3: a single-punch day at 06:30


def test_punches_attributed_outside_the_window_are_dropped():
    night_worker = att.Employee(key="EMP001", shift=shift(cases.NIGHT), joined_on=date(2026, 1, 1))
    punches = [raw(1, "MARS-01", "1", DAY, "05:00")]  # belongs to 31 August
    assert day_of(run(punches, employees=[night_worker])).status == att.Status.ABSENT


def test_affected_work_dates():
    night = shift(cases.NIGHT)
    instants = [aware(cases.at(DAY, "22:00")), aware(cases.at(cases.NEXT_DAY, "07:00")), aware(cases.at(cases.NEXT_DAY, "12:00"))]
    assert att.affected_work_dates(instants, shift=night, timezone=cases.TIMEZONE) == {DAY, cases.NEXT_DAY}
    assert att.affected_work_dates(instants, shift=None, timezone=cases.TIMEZONE) == {DAY, cases.NEXT_DAY}
    assert att.affected_work_dates([], shift=None, timezone=cases.TIMEZONE) == set()


# ---------------------------------------------------------------------------------------------------------------
# A7 — future-date guard; A10 — office timezone
# ---------------------------------------------------------------------------------------------------------------


def test_no_row_is_written_for_today_or_later():
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)  # 11:30 on 3 September in India
    result = run(date_from=DAY, date_to=date(2026, 9, 5), now=now)
    assert {write.result.work_date for write in result.writes} == {DAY, date(2026, 9, 2)}
    assert result.skipped_future == (("EMP001", date(2026, 9, 3)), ("EMP001", date(2026, 9, 4)), ("EMP001", date(2026, 9, 5)))
    assert result.counts()["skipped_future"] == 3


def test_the_first_push_of_the_day_stores_no_absence():
    """v3: the first ADMS push recomputed the day for everybody and stored ABSENT for all who had not punched yet."""
    now = datetime(2026, 9, 1, 3, 35, tzinfo=UTC)  # 09:05 IST
    result = run([raw(1, "MARS-01", "1", DAY, "09:02")], employees=[AROMAL, BINDU], now=now)
    assert result.writes == ()
    assert len(result.skipped_future) == 2


def test_today_follows_each_employees_office_timezone():
    now = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)  # already 2 September in India, still 1 September in London
    london = att.Employee(key="LON", timezone="Europe/London", shift=GEN_SHIFT, joined_on=date(2026, 1, 1))
    result = run(employees=[AROMAL, london], now=now)
    assert [write.employee for write in result.writes] == ["EMP001"]
    assert result.skipped_future == (("LON", DAY),)


def test_work_dates_follow_the_office_wall_clock():
    """A punch at 23:30 UTC on 31 August is 05:00 on 1 September in India."""
    punch = att.RawPunch(1, "MARS-01", "1", datetime(2026, 8, 31, 23, 30, tzinfo=UTC))
    day = day_of(run([punch]))
    assert day.first_in == datetime(2026, 9, 1, 5, 0)


# ---------------------------------------------------------------------------------------------------------------
# A12 — corrections
# ---------------------------------------------------------------------------------------------------------------


def test_a_corrected_day_is_never_overwritten():
    result = run([raw(1, "MARS-01", "1", DAY, "09:00")], corrected=[("EMP001", DAY)])
    assert day_of(result) is None
    assert result.skipped_corrected == (("EMP001", DAY),)
    assert result.counts() == {"written": 0, "skipped_corrected": 1, "skipped_future": 0, "absent_days": 0, "raw_punches_considered": 1, "unmapped_pins": 0}


def test_apply_correction():
    day = att.compute_day(DAY, [att.Punch(1, aware(cases.at(DAY, "09:00")))], GEN_SHIFT)
    assert att.apply_correction(day, "status", "ON_LEAVE").status == att.Status.ON_LEAVE
    closed = att.apply_correction(day, "last_out", datetime(2026, 9, 1, 18, 0))
    assert closed.last_out == datetime(2026, 9, 1, 18, 0) and closed.missing_out is False
    assert att.apply_correction(day, "working_minutes", 480).working_minutes == 480
    assert att.apply_correction(day, "is_late", True).is_late is True
    assert att.apply_correction(day, "first_in", None).first_in is None
    for field, value in (
        ("status", "SICK"),
        ("punch_count", 3),
        ("working_minutes", -1),
        ("working_minutes", "480"),
        ("is_late", "yes"),
        ("last_out", datetime(2026, 9, 1, 8, 0)),
        ("first_in", aware(cases.at(DAY, "09:00"))),
    ):
        with pytest.raises(ValueError):
            att.apply_correction(day, field, value)
    no_in = att.compute_day(DAY, [], GEN_SHIFT)
    with pytest.raises(ValueError):
        att.apply_correction(no_in, "last_out", datetime(2026, 9, 1, 18, 0))


def test_rules_apply_per_employee_inside_a_recompute():
    rules = [att.AttendanceRule(rules={"half_day_after": "11:30"}, office="MAIN")]
    punches = [raw(1, "MARS-01", "1", DAY, "11:00"), raw(2, "MARS-01", "1", DAY, "15:00")]
    assert day_of(run(punches)).status == att.Status.HALF_DAY  # default deadline 09:30
    assert day_of(run(punches, rules=rules)).status == att.Status.LATE


def test_context_lookup_helper():
    lookup = att.context_lookup(att.HolidayCalendar([att.Holiday(DAY, "Onam")]), att.LeaveBook(), AROMAL)
    assert lookup(DAY).is_holiday is True and lookup(SUNDAY).is_holiday is False
    assert att.HolidayCalendar([att.Holiday(DAY, "Own", office="MAIN"), att.Holiday(DAY, "All")]).name("MAIN", DAY) == "Own"
    assert att.HolidayCalendar([att.Holiday(DAY, "All")]).name(None, DAY) == "All"


def test_unmapped_pins_helper():
    identity = att.build_identity_map(LINKS)
    punches = [raw(1, "MARS-01", "1", DAY, "09:00"), raw(2, "MARS-01", "7", DAY, "09:00"), raw(3, "MARS-01", "7", DAY, "10:00"), raw(4, "GATE", "2", DAY, "10:00")]
    assert att.unmapped_pins(punches, identity) == {"MARS-01": ("7",), "GATE": ("2",)}


def test_raw_punch_pins_are_text():
    assert att.RawPunch(1, "D", 7, datetime(2026, 9, 1, 3, 30, tzinfo=UTC)).pin == "7"
    assert att.RawPunch(1, "D", " 7 ", datetime(2026, 9, 1, 3, 30, tzinfo=UTC)).pin == "7"


def test_employed_on():
    employee = att.Employee(key=1, joined_on=date(2026, 9, 2), left_on=date(2026, 9, 3))
    assert [employee.employed_on(date(2026, 9, d)) for d in (1, 2, 3, 4)] == [False, True, True, False]
    assert att.Employee(key=2).employed_on(date(1990, 1, 1)) is True


def test_a_day_of_punches_without_a_shift_uses_the_v3_fallbacks():
    free = att.Employee(key="EMP001", shift=None)
    result = run([raw(1, "MARS-01", "1", DAY, "09:00"), raw(2, "MARS-01", "1", DAY, "17:00")], employees=[free])
    day = day_of(result)
    assert (day.status, day.working_minutes, day.break_minutes, day.late_minutes) == (att.Status.PRESENT, 480, 0, 0)
    assert day_of(run(employees=[free], date_from=SUNDAY, date_to=SUNDAY), day=SUNDAY).status == att.Status.WEEKLY_OFF


def test_recompute_accepts_a_generator_of_punches():
    punches = (raw(i, "MARS-01", "1", DAY, clock) for i, clock in enumerate(("09:00", "18:00"), start=1))
    result = run(punches)
    assert day_of(result).punch_count == 2 and result.raw_punches_considered == 2


def test_time_helper_used_by_the_cases():
    assert cases.at(DAY, "09:00:30") == datetime.combine(DAY, time(9, 0, 30))
