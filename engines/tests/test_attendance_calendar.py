"""engines.attendance calendar fill and summaries (A7, A9) — ported from the calendar cases of the eSSL
``test_status_reports_calendar.py``, ``test_half_day_and_in_out.py``, ``test_multi_device_attendance.py`` and
``test_employee_list_active_default.py``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from engines import attendance as att
from engines.tests import attendance_cases as cases
from engines.tests.attendance_support import aware, shift

YEAR, MONTH = 2026, 9
DAY = cases.DAY
GEN_SHIFT = shift(cases.GEN)
EMPLOYEE = att.Employee(key="EMP001", office="MAIN", shift=GEN_SHIFT, joined_on=date(2026, 1, 1))
MONTH_OVER = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


def worked(day, *clocks, the_shift=GEN_SHIFT, context=None):
    punches = [att.Punch(i, aware(cases.at(day, clock))) for i, clock in enumerate(clocks, start=1)]
    return att.compute_day(day, punches, the_shift, context)


def month(stored=None, employee=EMPLOYEE, now=MONTH_OVER, holidays=None, leaves=None):
    first, last = att.month_bounds(YEAR, MONTH)
    return att.calendar_fill(employee, date_from=first, date_to=last, now=now, stored=stored or {}, holidays=holidays, leaves=leaves)


def on(days, day):
    return next(cell for cell in days if cell.date == day)


def test_every_date_of_the_month_is_present():
    days = month()
    assert len(days) == 30
    assert [cell.date.day for cell in days] == list(range(1, 31))


def test_weekly_offs_are_shown_rather_than_gaps():
    sunday = on(month(), cases.SUNDAY)
    assert (sunday.status, sunday.status_code, sunday.stored, sunday.fill) == ("WEEKLY_OFF", "WO", False, att.Fill.FILLED)


def test_a_stored_day_keeps_first_in_last_out_and_its_punch_count():
    day = worked(DAY, "09:04", "13:00", "18:11")
    cell = on(month({DAY: day}), DAY)
    assert (cell.first_in_label, cell.last_out_label, cell.punch_count, cell.stored) == ("09:04", "18:11", 3, True)
    assert cell.missing_out is False


def test_a_single_punch_says_the_out_is_missing():
    cell = on(month({DAY: worked(DAY, "09:04")}), DAY)
    assert (cell.first_in_label, cell.last_out_label, cell.missing_out) == ("09:04", att.MISSING_OUT_LABEL, True)
    assert cell.as_dict()["last_out"] == "Missing OUT"


def test_an_absence_is_not_a_missing_out():
    cell = on(month(), DAY)
    assert (cell.status, cell.last_out_label, cell.missing_out) == ("ABSENT", "", False)


@pytest.mark.parametrize("clocks", [("09:00", "18:05"), ("10:30", "15:00"), ("09:00", "14:00"), ("09:41", "19:00")])
def test_the_calendar_reads_a_day_exactly_as_it_was_computed(clocks):
    day = worked(DAY, *clocks)
    cell = on(month({DAY: day}), DAY)
    assert cell.status == day.status
    assert (cell.first_in, cell.last_out) == (day.first_in, day.last_out)


def test_a_missing_day_is_decided_exactly_as_compute_day_decides_a_day_without_punches():
    holidays = att.HolidayCalendar([att.Holiday(date(2026, 9, 2), "Onam")])
    leaves = att.LeaveBook([att.Leave("EMP001", date(2026, 9, 3), date(2026, 9, 3), leave_type="CL")])
    days = month(holidays=holidays, leaves=leaves)
    for cell in days:
        ctx = att.day_context("EMP001", "MAIN", cell.date, holidays, leaves)
        assert cell.status == att.compute_day(cell.date, [], GEN_SHIFT, ctx).status
    assert on(days, date(2026, 9, 2)).holiday_name == "Onam"
    assert on(days, date(2026, 9, 3)).leave_type == "CL"
    assert on(days, date(2026, 9, 3)).status_code == "L"


def test_future_days_are_blank_not_absent():
    now = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    days = month(now=now)
    assert on(days, date(2026, 9, 14)).status == "ABSENT"
    today, tomorrow = on(days, date(2026, 9, 15)), on(days, date(2026, 9, 16))
    assert (today.status, today.fill, tomorrow.status, tomorrow.fill, tomorrow.status_code) == ("", att.Fill.PENDING, "", att.Fill.PENDING, "")
    summary = att.summarise(days)
    assert summary["pending_days"] == 16
    assert summary["absent"] == 12  # v3 filled all 26 working days of the month as ABSENT (A7)


def test_days_outside_employment_are_blank():
    employee = att.Employee(key="EMP001", shift=GEN_SHIFT, joined_on=date(2026, 9, 10), left_on=date(2026, 9, 20))
    days = month(employee=employee, holidays=att.HolidayCalendar([att.Holiday(date(2026, 9, 2), "Onam")]))
    assert on(days, date(2026, 9, 2)).status == ""  # v3 showed the holiday before the person joined
    assert on(days, date(2026, 9, 2)).fill == att.Fill.NOT_EMPLOYED
    assert on(days, date(2026, 9, 21)).fill == att.Fill.NOT_EMPLOYED
    assert att.summarise(days)["not_employed_days"] == 19


def test_calendar_fill_writes_nothing_and_takes_row_like_objects():
    class Row:  # an ORM row: attribute names of attendance_day
        status = "PRESENT"
        first_in = datetime(2026, 9, 1, 9, 0)
        last_out = datetime(2026, 9, 1, 18, 0)
        punch_count = 2
        working_minutes = 480
        overtime_minutes = 0
        late_minutes = 0
        early_exit_minutes = 0
        is_corrected = True

    stored = {DAY: Row()}
    cell = on(month(stored), DAY)
    assert (cell.status, cell.working_minutes, cell.is_corrected, cell.worked_on_off_day) == ("PRESENT", 480, True, False)
    assert list(stored) == [DAY]


def test_the_monthly_summary_counts_what_the_calendar_draws():
    stored = {DAY: worked(DAY, "10:30", "15:00"), date(2026, 9, 2): worked(date(2026, 9, 2), "09:00", "18:10")}
    summary = att.summarise(month(stored))
    assert (summary["half_day"], summary["present"]) == (1, 1)
    assert summary["present_days"] == summary["present"] + summary["late"] + summary["half_day"] == 2
    assert summary["absent_days"] == summary["absent"]


def test_a_half_day_is_counted_as_a_day_present():
    stored = {DAY: worked(DAY, "10:30", "18:00", the_shift=shift({**cases.GEN, "start_time": cases.SEED["start_time"]}))}
    summary = att.summarise(month(stored))
    assert (summary["half_day"], summary["present_days"]) == (1, 1)
    assert att.counts_as_present(stored[DAY].status)


def test_monthly_report_consolidates_every_device():
    second = DAY + timedelta(days=1)
    day_one = att.compute_day(DAY, [att.Punch(1, aware(cases.at(DAY, "09:00")), "MARS-01"), att.Punch(2, aware(cases.at(DAY, "18:30")), "MARS-02")], GEN_SHIFT)
    day_two = att.compute_day(second, [att.Punch(3, aware(cases.at(second, "09:00")), "MARS-02"), att.Punch(4, aware(cases.at(second, "18:30")), "MARS-01")], GEN_SHIFT)
    summary = att.summarise(month({DAY: day_one, second: day_two}))
    assert summary["present"] == 2
    counted = sum(summary[key] for key in ("present", "absent", "late", "half_day", "weekly_off", "holiday", "leave"))
    assert counted == 30  # every day of September accounted for once


def test_status_codes_are_unambiguous():
    assert att.STATUS_CODE == {"PRESENT": "P", "LATE": "LT", "ABSENT": "A", "HALF_DAY": "HD", "WEEKLY_OFF": "WO", "HOLIDAY": "H", "ON_LEAVE": "L"}
    assert len(set(att.STATUS_CODE.values())) == len(att.STATUS_CODE)
    assert set(att.STATUS_LABEL) == set(att.Status)


# ---------------------------------------------------------------------------------------------------------------
# A9 — one attendance-rate formula
# ---------------------------------------------------------------------------------------------------------------


def test_attendance_rate_formula():
    assert att.attendance_rate(present=18, late=2, half_day=2, absent=2) == Decimal("0.8750")  # (20 + 1) / 24
    assert att.attendance_rate(present=0, late=0, half_day=1, absent=0) == Decimal("0.5000")
    assert att.attendance_rate(present=0, late=0, half_day=0, absent=0) is None
    assert att.attendance_rate(present=1, late=0, half_day=0, absent=2) == Decimal("0.3333")


def test_leave_holidays_and_weekly_offs_are_not_expected_days():
    leaves = att.LeaveBook([att.Leave("EMP001", date(2026, 9, 2), date(2026, 9, 4))])
    holidays = att.HolidayCalendar([att.Holiday(date(2026, 9, 7), "Onam")])
    stored = {DAY: worked(DAY, "09:00", "18:00"), date(2026, 9, 8): worked(date(2026, 9, 8), "10:30", "15:00")}
    summary = att.summarise(month(stored, holidays=holidays, leaves=leaves))
    # 26 working days − 3 leave − 1 holiday = 22 expected; 1 present, 1 half day, 20 absent.
    assert (summary["leave"], summary["holiday"], summary["expected_days"]) == (3, 1, 22)
    assert summary["attendance_rate"] == Decimal("0.0682")  # (1 + 0.5) / 22


def test_worked_off_days_are_counted_separately():
    stored = {cases.SUNDAY: worked(cases.SUNDAY, "09:00", "13:00")}
    summary = att.summarise(month(stored))
    assert (summary["weekly_off"], summary["worked_off_days"], summary["present_days"], summary["overtime_minutes"]) == (4, 1, 0, 240)
    assert summary["overtime_hours"] == "4:00"


def test_leave_conflicts_are_counted():
    stored = {DAY: worked(DAY, "09:00", "18:00", context=att.DayContext(on_leave=True))}
    assert att.summarise(month(stored))["leave_conflicts"] == 1


def test_office_rate_pools_the_counts_with_the_same_formula():
    one = att.summarise(month({DAY: worked(DAY, "09:00", "18:00")}))
    two = att.summarise(month({DAY: worked(DAY, "10:30", "15:00")}))
    office = att.combine_summaries([one, two])
    assert office["expected_days"] == one["expected_days"] + two["expected_days"] == 52
    assert office["attendance_rate"] == att.attendance_rate(present=1, late=0, half_day=1, absent=50)
    assert office["working_minutes"] == 720 and office["working_hours"] == "12:00"


def test_totals_by_date_match_the_grid():
    other = att.Employee(key="EMP002", office="MAIN", shift=GEN_SHIFT, joined_on=date(2026, 1, 1))
    first = month({DAY: worked(DAY, "09:04", "18:11")})
    second = month(employee=other)
    columns = att.totals_by_date([first, second])
    column = columns[0]
    assert column["date"] == "2026-09-01" and column["day"] == "Tue"
    assert (column["present"], column["absent"], column["stored"]) == (1, 1, 1)
    assert column["present"] + column["late"] == sum(1 for grid in (first, second) if grid[0].status in ("PRESENT", "LATE"))
    assert columns[5]["weekly_off"] == 2
    assert att.totals_by_date([]) == []
    with pytest.raises(ValueError):
        att.totals_by_date([first, first[1:] + first[:1]])


def test_calendar_day_as_dict_and_labels():
    data = on(month({DAY: worked(DAY, "09:04", "18:11")}), DAY).as_dict()
    assert data["date"] == "2026-09-01" and data["day"] == "Tue" and data["status_code"] == "P" and data["status_label"] == "Present"
    assert data["first_in"] == "09:04" and data["last_out"] == "18:11" and data["working_hours"] == "8:07" and data["stored"] is True


def test_month_bounds_and_hm():
    assert att.month_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert att.month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))
    assert att.month_bounds(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))
    assert att.hm(0) == "0:00" and att.hm(None) == "0:00" and att.hm(475) == "7:55" and att.hm(600) == "10:00"
