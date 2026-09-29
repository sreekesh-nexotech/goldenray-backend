"""Attendance reports (``attendance.export``): daily, weekly, monthly, monthly-detail, monthly-individual, individual,
office — one builder per report feeding JSON and every file format.

A9, against eSSL (§I.16): **every** report reads the one calendar fill (:mod:`attendance.services.calendar`), so a
day reads the same in the daily report, the weekly grid, the monthly figures and the calendar (eSSL's daily, weekly,
individual and office reports read stored rows only); **one** attendance-% formula (``engines.attendance.attendance_rate``:
``(present + late + 0.5 × half day) / (present + late + half day + absent)``, leave, holidays and weekly offs outside
the denominator) for a person, an office and the overall line alike; **unambiguous** status codes (``P LT A HD WO H L``
— the weekly grid no longer prints ``H`` for both holiday and half day); today and later are blank, never absent (A7).
The people covered are the caller's attendance scope; deactivated employees only with ``include_inactive`` or when
named.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from attendance.services import calendar
from core.errors import DomainError
from engines import attendance as engine

MAX_RANGE_DAYS = 366


@dataclass
class Report:
    kind: str
    title: str
    subtitle: str
    columns: list[tuple[str, str]]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] = field(default_factory=dict)
    filename: str = "attendance-report"

    @property
    def headers(self) -> list[str]:
        return [header for _, header in self.columns]

    def matrix(self) -> list[list[Any]]:
        return [[row.get(key, "") for key, _ in self.columns] for row in self.rows]

    def as_json(self) -> dict:
        return {
            "report": self.kind,
            "title": self.title,
            "subtitle": self.subtitle,
            "columns": [{"key": key, "header": header} for key, header in self.columns],
            "rows": self.rows,
            "totals": self.totals,
            "row_count": len(self.rows),
        }


def _office_label(office) -> str:
    return office.name if office is not None else "All offices"


def _shift_name(employee) -> str:
    shift = employee.effective_shift
    return shift.name if shift is not None else ""


def _person_line(employee, pins: list[str]) -> str:
    office = employee.office.name if employee.office_id else "No office"
    return f"{employee.code}  |  {office}  |  {_shift_name(employee) or 'No shift'}  |  device PINs {', '.join(pins) or 'not linked'}"


def _code(cell) -> str:
    return engine.STATUS_CODE.get(cell.status, "")


def _day_row(employee, cell, *, with_employee: bool = True) -> dict:
    row = {
        "work_date": cell.date.isoformat(),
        "weekday": cell.date.strftime("%a"),
        "first_in": cell.first_in_label,
        "last_out": cell.last_out_label,
        "punches": cell.punch_count,
        "working_hours": engine.hm(cell.working_minutes),
        "late": engine.hm(cell.late_minutes) if cell.late_minutes else "",
        "early_exit": engine.hm(cell.early_exit_minutes) if cell.early_exit_minutes else "",
        "overtime": engine.hm(cell.overtime_minutes) if cell.overtime_minutes else "",
        "status": _code(cell),
        "status_label": cell.status_label or ("Pending" if cell.fill == engine.Fill.PENDING else ""),
        "corrected": "yes" if cell.is_corrected else "",
    }
    if with_employee:
        row.update(employee_code=employee.code, employee_name=employee.full_name, office=employee.office.name if employee.office_id else "", shift=_shift_name(employee))
    return row


def _status_totals(cells) -> dict:
    summary = engine.summarise(cells)
    return {
        "Present Days": summary["present_days"],
        "Absent Days": summary["absent_days"],
        "Present": summary["present"],
        "Late": summary["late"],
        "Absent": summary["absent"],
        "Half Day": summary["half_day"],
        "Weekly Off": summary["weekly_off"],
        "Holiday": summary["holiday"],
        "Leave": summary["leave"],
        "Attendance %": _pct(summary["attendance_rate"]),
        "Total Hours": summary["working_hours"],
        "Overtime": summary["overtime_hours"],
    }


def _pct(rate) -> str:
    return "" if rate is None else f"{(rate * 100).quantize(Decimal('0.1'))}%"


def _check_range(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise DomainError("validation_error", "date_to is before date_from.", errors={"date_to": ["Must not be before date_from."]})
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise DomainError("range_too_long", f"A report covers at most {MAX_RANGE_DAYS} days.", errors={"date_to": [f"At most {MAX_RANGE_DAYS} days."]})


DAY_COLUMNS = [
    ("first_in", "First In"),
    ("last_out", "Last Out"),
    ("punches", "Punches"),
    ("working_hours", "Working Hours"),
    ("late", "Late"),
    ("early_exit", "Early Exit"),
    ("overtime", "Overtime"),
    ("status", "Status"),
    ("status_label", "Status Detail"),
    ("corrected", "Corrected"),
]
EMPLOYEE_COLUMNS = [("employee_code", "Employee ID"), ("employee_name", "Employee")]


# --------------------------------------------------------------------------------------------------------------------
def daily(employees, day: date, *, office=None, status: str | None = None) -> Report:
    employees = list(employees)
    cells = calendar.fill(employees, day, day)
    rows, shown = [], []
    for employee in employees:
        cell = cells[employee.pk][0]
        if status and cell.status != status:
            continue
        shown.append(cell)
        rows.append(_day_row(employee, cell))
    return Report(
        kind="daily",
        title="Daily Attendance Report",
        subtitle=f"{day.isoformat()}  |  {_office_label(office)}" + (f"  |  status {engine.STATUS_LABEL.get(status, status)}" if status else ""),
        columns=[*EMPLOYEE_COLUMNS, ("office", "Office"), ("shift", "Shift"), *DAY_COLUMNS],
        rows=rows,
        totals={"Employees": len(rows), **_status_totals(shown)},
        filename=f"daily-attendance-{day.isoformat()}",
    )


def weekly(employees, week_start: date, *, office=None) -> Report:
    employees = list(employees)
    week_end = week_start + timedelta(days=6)
    cells = calendar.fill(employees, week_start, week_end)
    days = [week_start + timedelta(days=offset) for offset in range(7)]
    rows = []
    for employee in employees:
        summary = engine.summarise(cells[employee.pk])
        row = {"employee_code": employee.code, "employee_name": employee.full_name, "office": employee.office.name if employee.office_id else ""}
        for cell in cells[employee.pk]:
            # H:MM when time was worked, else the unambiguous status code (A9)
            row[cell.date.isoformat()] = engine.hm(cell.working_minutes) if cell.working_minutes else _code(cell)
        row.update(
            present_days=summary["present_days"],
            absent_days=summary["absent_days"],
            half_day=summary["half_day"],
            late=summary["late"],
            attendance_pct=_pct(summary["attendance_rate"]),
            total_hours=summary["working_hours"],
            overtime=summary["overtime_hours"],
        )
        rows.append(row)
    columns = [*EMPLOYEE_COLUMNS, ("office", "Office")]
    columns += [(day.isoformat(), day.strftime("%a %d")) for day in days]
    columns += [
        ("present_days", "Present Days"),
        ("absent_days", "Absent Days"),
        ("half_day", "Half Day"),
        ("late", "Late"),
        ("attendance_pct", "Attendance %"),
        ("total_hours", "Total Hours"),
        ("overtime", "Overtime"),
    ]
    combined = engine.combine_summaries(engine.summarise(cells[employee.pk]) for employee in employees)
    return Report(
        kind="weekly",
        title="Weekly Attendance Report",
        subtitle=f"{week_start.isoformat()} to {week_end.isoformat()}  |  {_office_label(office)}",
        columns=columns,
        rows=rows,
        totals={"Employees": len(rows), "Attendance %": _pct(combined["attendance_rate"]), "Codes": "P present · LT late · HD half day · A absent · WO weekly off · H holiday · L leave"},
        filename=f"weekly-attendance-{week_start.isoformat()}",
    )


def monthly(employees, year: int, month: int, *, office=None) -> Report:
    employees = list(employees)
    first, last = engine.month_bounds(year, month)
    cells = calendar.fill(employees, first, last)
    rows, summaries = [], []
    for employee in employees:
        summary = engine.summarise(cells[employee.pk])
        summaries.append(summary)
        rows.append(
            {
                "employee_code": employee.code,
                "employee_name": employee.full_name,
                "office": employee.office.name if employee.office_id else "",
                "present_days": summary["present_days"],
                "absent_days": summary["absent_days"],
                "present": summary["present"],
                "late": summary["late"],
                "absent": summary["absent"],
                "half_day": summary["half_day"],
                "weekly_off": summary["weekly_off"],
                "holiday": summary["holiday"],
                "leave": summary["leave"],
                "attendance_pct": _pct(summary["attendance_rate"]),
                "working_hours": summary["working_hours"],
                "overtime_hours": summary["overtime_hours"],
            }
        )
    combined = engine.combine_summaries(summaries)
    return Report(
        kind="monthly",
        title="Monthly Attendance Report",
        subtitle=f"{first.strftime('%B %Y')}  |  {_office_label(office)}",
        columns=[
            *EMPLOYEE_COLUMNS,
            ("office", "Office"),
            ("present_days", "Present Days"),
            ("absent_days", "Absent Days"),
            ("present", "Present"),
            ("late", "Late"),
            ("absent", "Absent"),
            ("half_day", "Half Day"),
            ("weekly_off", "Weekly Off"),
            ("holiday", "Holiday"),
            ("leave", "Leave"),
            ("attendance_pct", "Attendance %"),
            ("working_hours", "Total Working Hours"),
            ("overtime_hours", "Overtime"),
        ],
        rows=rows,
        totals={"Employees": len(rows), "Days": (last - first).days + 1, "Attendance %": _pct(combined["attendance_rate"]), "Total Hours": combined["working_hours"]},
        filename=f"monthly-attendance-{year}-{month:02d}",
    )


def monthly_detail(employees, year: int, month: int, *, office=None) -> Report:
    employees = list(employees)
    first, last = engine.month_bounds(year, month)
    cells = calendar.fill(employees, first, last)
    rows = [_day_row(employee, cell) for employee in employees for cell in cells[employee.pk]]
    return Report(
        kind="monthly-detail",
        title="Monthly Attendance Detail",
        subtitle=f"{first.strftime('%B %Y')}  |  {_office_label(office)}",
        columns=[*EMPLOYEE_COLUMNS, ("work_date", "Date"), ("weekday", "Day"), *DAY_COLUMNS],
        rows=rows,
        totals={"Employees": len(employees), "Rows": len(rows)},
        filename=f"monthly-detail-{year}-{month:02d}",
    )


def monthly_individual(employee, year: int, month: int, *, pins: list[str]) -> Report:
    first, last = engine.month_bounds(year, month)
    cells = calendar.fill([employee], first, last)[employee.pk]
    rows = [{**_day_row(employee, cell), "department": employee.department} for cell in cells]
    return Report(
        kind="monthly-individual",
        title=f"Monthly Attendance - {employee.full_name}",
        subtitle=f"{_person_line(employee, pins)}  |  {first.strftime('%B %Y')}",
        columns=[*EMPLOYEE_COLUMNS, ("department", "Department"), ("work_date", "Date"), ("weekday", "Day"), *DAY_COLUMNS],
        rows=rows,
        totals=_status_totals(cells),
        filename=f"employee-{employee.code}-{year}-{month:02d}",
    )


def individual(employees, date_from: date, date_to: date, *, employee=None, office=None, pins: list[str] | None = None) -> Report:
    _check_range(date_from, date_to)
    employees = [employee] if employee is not None else list(employees)
    cells = calendar.fill(employees, date_from, date_to)
    rows, shown = [], []
    for person in employees:
        for cell in cells[person.pk]:
            if cell.fill == engine.Fill.NOT_EMPLOYED:
                continue
            shown.append(cell)
            rows.append(_day_row(person, cell, with_employee=employee is None) | {"shift": _shift_name(person)})
    if employee is not None:
        title = f"Attendance Report - {employee.full_name}"
        subtitle = f"{_person_line(employee, pins or [])}  |  {date_from.isoformat()} to {date_to.isoformat()}"
        columns = [("work_date", "Date"), ("weekday", "Day"), ("shift", "Shift"), *DAY_COLUMNS]
        filename = f"employee-{employee.code}-{date_from.isoformat()}-{date_to.isoformat()}"
        totals = {"Days": len(rows), **_status_totals(shown)}
    else:
        title = "Attendance Report - All Employees"
        subtitle = f"{_office_label(office)}  |  {len(employees)} employee(s)  |  {date_from.isoformat()} to {date_to.isoformat()}"
        columns = [*EMPLOYEE_COLUMNS, ("work_date", "Date"), ("weekday", "Day"), ("shift", "Shift"), *DAY_COLUMNS]
        filename = f"attendance-{date_from.isoformat()}-{date_to.isoformat()}"
        totals = {"Employees": len(employees), "Days": len(rows), **_status_totals(shown)}
    return Report(kind="individual", title=title, subtitle=subtitle, columns=columns, rows=rows, totals=totals, filename=filename)


def office_report(employees, offices, date_from: date, date_to: date) -> Report:
    """One row per office: the pooled calendar of its people (same formula as a person's %, A9)."""
    _check_range(date_from, date_to)
    employees = list(employees)
    cells = calendar.fill(employees, date_from, date_to)
    rows, summaries = [], []
    for office in offices:
        members = [employee for employee in employees if employee.office_id == office.pk]
        pooled = [cell for employee in members for cell in cells[employee.pk]]
        summary = engine.summarise(pooled)
        summaries.append(summary)
        worked = summary["present"] + summary["late"] + summary["half_day"]
        rows.append(
            {
                "office": office.name,
                "office_code": office.code,
                "employees": len(members),
                "expected_days": summary["expected_days"],
                "present": summary["present"] + summary["late"],
                "late": summary["late"],
                "absent": summary["absent"],
                "half_day": summary["half_day"],
                "leave": summary["leave"],
                "early_exit": sum(1 for cell in pooled if cell.early_exit_minutes),
                "attendance_pct": _pct(summary["attendance_rate"]),
                "total_hours": summary["working_hours"],
                "avg_hours": engine.hm(round(summary["working_minutes"] / worked)) if worked else engine.hm(0),
            }
        )
    combined = engine.combine_summaries(summaries) if summaries else engine.combine_summaries([])
    return Report(
        kind="office",
        title="Office Attendance Report",
        subtitle=f"{date_from.isoformat()} to {date_to.isoformat()}  |  {len(rows)} office(s)",
        columns=[
            ("office", "Office"),
            ("office_code", "Code"),
            ("employees", "Employees"),
            ("expected_days", "Expected Days"),
            ("present", "Present (incl. late)"),
            ("late", "Late"),
            ("absent", "Absent"),
            ("half_day", "Half Day"),
            ("leave", "Leave"),
            ("early_exit", "Early Exit"),
            ("attendance_pct", "Attendance %"),
            ("total_hours", "Total Hours"),
            ("avg_hours", "Avg Hours/Day"),
        ],
        rows=rows,
        totals={
            "Offices": len(rows),
            "Expected Days": combined["expected_days"],
            "Present": combined["present"] + combined["late"],
            "Absent": combined["absent"],
            "Half Day": combined["half_day"],
            "Attendance %": _pct(combined["attendance_rate"]),
            "Total Hours": combined["working_hours"],
        },
        filename=f"office-attendance-{date_from.isoformat()}-{date_to.isoformat()}",
    )
