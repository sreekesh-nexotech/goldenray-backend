"""The one calendar-fill source (A9) behind calendars, rosters, reports and dashboards.

:func:`fill` gives every date of a range for a list of employees in one pass: the stored day where there is one, else
the day decided exactly as the engine decides a day without punches (leave > holiday > weekly off > absent), blank
before joining / after leaving, and **blank for today and later** (A7: v3 showed future working days as ABSENT).
:func:`provisional_today` computes today's day from the punches received so far, in memory only (nothing is
stored before the day is final) — used by the day roster and the dashboard, never by reports.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from attendance.models import AttendanceDay
from attendance.services import inputs
from attendance.services.common import now, office_timezone, today_for
from core.errors import DomainError
from engines import attendance as engine

MAX_ROSTER_EMPLOYEES = 300
MAX_DAY_EMPLOYEES = 1000  # one row each on attendance/day/ (never truncated: narrow by office or search)
PROVISIONAL = "PROVISIONAL"


def stored_days(employee_ids, date_from: date, date_to: date) -> dict[int, dict[date, AttendanceDay]]:
    by_employee: dict[int, dict[date, AttendanceDay]] = defaultdict(dict)
    for day in AttendanceDay.objects.filter(employee_id__in=list(employee_ids), work_date__gte=date_from, work_date__lte=date_to).order_by():
        by_employee[day.employee_id][day.work_date] = day
    return by_employee


def fill(employees, date_from: date, date_to: date, *, at: datetime | None = None, with_rows: bool = False):
    """``{employee pk: [CalendarDay, …]}`` (and the stored rows when ``with_rows``) — three queries for any roster."""
    at = at or now()
    employees = list(employees)
    ids = [employee.pk for employee in employees]
    stored = stored_days(ids, date_from, date_to)
    holidays, leaves = inputs.calendars(ids, date_from, date_to)
    people = {person.key: person for person in inputs.engine_employees(employees)}
    result = {
        employee.pk: engine.calendar_fill(people[employee.pk], date_from=date_from, date_to=date_to, now=at, stored=stored.get(employee.pk, {}), holidays=holidays, leaves=leaves)
        for employee in employees
    }
    return (result, stored) if with_rows else result


def provisional_today(employees, day: date, *, at: datetime | None = None) -> dict[int, engine.CalendarDay]:
    """Today's days computed from the punches so far (not stored; A7). Nobody in yet → blank (PENDING), never ABSENT."""
    at = at or now()
    employees = list(employees)
    ids = [employee.pk for employee in employees]
    staff = inputs.engine_employees(employees)
    links = inputs.links(ids)
    identity = engine.build_identity_map(links)
    punches: dict[int, list[engine.Punch]] = defaultdict(list)
    people = {person.key: person for person in staff}
    for raw in inputs.raw_punches(links, day, day):
        person = people.get(identity.get((raw.device, raw.pin)))
        if person is None or raw.punch_at > at:
            continue
        if engine.attribute_work_date(engine.local_wall_clock(raw.punch_at, person.timezone), person.shift) == day:
            punches[person.key].append(engine.Punch(raw_id=raw.raw_id, punch_at=raw.punch_at, device=raw.device))
    holidays, leaves = inputs.calendars(ids, day, day)
    rules = inputs.rules()
    result = {}
    for person in staff:
        context = engine.day_context(person.key, person.office, day, holidays, leaves)
        if not person.employed_on(day):
            result[person.key] = engine.CalendarDay(date=day, status="", fill=engine.Fill.NOT_EMPLOYED)
            continue
        if not punches[person.key]:
            result[person.key] = engine.CalendarDay(date=day, status="", fill=engine.Fill.PENDING, holiday_name=context.holiday_name, leave_type=context.leave_type)
            continue
        day_rules = engine.rules_for(rules, office=person.office, shift=person.shift, work_date=day)
        computed = engine.compute_day(day, punches[person.key], person.shift, context, day_rules, timezone=person.timezone)
        result[person.key] = engine.CalendarDay(
            date=day,
            status=str(computed.status),
            fill=PROVISIONAL,
            first_in=computed.first_in,
            last_out=computed.last_out,
            punch_count=computed.punch_count,
            working_minutes=computed.working_minutes,
            overtime_minutes=computed.overtime_minutes,
            late_minutes=computed.late_minutes,
            early_exit_minutes=computed.early_exit_minutes,
            worked_on_off_day=computed.worked_on_off_day,
            leave_conflict=computed.leave_conflict,
            holiday_name=context.holiday_name,
            leave_type=context.leave_type,
        )
    return result


def day_cells(employees, day: date, *, at: datetime | None = None) -> tuple[dict[int, engine.CalendarDay], dict]:
    """One cell per employee for ``day``: stored/filled for final days, provisional for each office's today."""
    at = at or now()
    employees = list(employees)
    current = [employee for employee in employees if day == today_for(employee.office, at=at)]
    others = [employee for employee in employees if employee not in current]
    cells, stored = fill(others, day, day, at=at, with_rows=True)
    cells = {pk: days[0] for pk, days in cells.items()}
    cells.update(provisional_today(current, day, at=at))
    rows = {pk: by_date.get(day) for pk, by_date in stored.items()}
    return cells, rows


def check_size(employees, limit: int) -> list:
    """The people of a roster, or 400 ``too_many_employees`` — a roster is never silently truncated."""
    employees = list(employees)
    if len(employees) > limit:
        raise DomainError(
            "too_many_employees",
            f"{len(employees)} employees match; this view draws at most {limit}. Narrow it by office or search.",
            errors={"office": [f"At most {limit} employees."]},
        )
    return employees


def month_view(employee, year: int, month: int, *, at: datetime | None = None) -> dict:
    first, last = engine.month_bounds(year, month)
    days = fill([employee], first, last, at=at)[employee.pk]
    return {"employee": employee, "year": year, "month": month, "month_label": first.strftime("%B %Y"), "date_from": first, "date_to": last, "days": days, "summary": engine.summarise(days)}


def roster(employees, year: int, month: int, *, at: datetime | None = None) -> dict:
    """Every employee's month on one calendar (at most :data:`MAX_ROSTER_EMPLOYEES`, else 400 — never truncated)."""
    employees = check_size(employees, MAX_ROSTER_EMPLOYEES)
    first, last = engine.month_bounds(year, month)
    calendars = fill(employees, first, last, at=at)
    rows = [{"employee": employee, "days": calendars[employee.pk], "summary": engine.summarise(calendars[employee.pk])} for employee in employees]
    ordered = [calendars[employee.pk] for employee in employees]
    return {
        "year": year,
        "month": month,
        "month_label": first.strftime("%B %Y"),
        "date_from": first,
        "date_to": last,
        "dates": [{"date": day.isoformat(), "day": day.strftime("%a"), "day_number": day.day} for day in (first + timedelta(days=i) for i in range((last - first).days + 1))],
        "employees": rows,
        "by_date": engine.totals_by_date(ordered),
        "totals": {"employees": len(rows), "days": (last - first).days + 1, **engine.combine_summaries(row["summary"] for row in rows)},
    }


def date_ranges(office=None, *, at: datetime | None = None) -> dict:
    """Today / yesterday / last 7 / last 30 days as office-local dates (A10)."""
    anchor = today_for(office, at=at)
    return {
        "timezone": office_timezone(office),
        "today": anchor,
        "ranges": {
            "today": {"date_from": anchor, "date_to": anchor},
            "yesterday": {"date_from": anchor - timedelta(days=1), "date_to": anchor - timedelta(days=1)},
            "last7": {"date_from": anchor - timedelta(days=6), "date_to": anchor},
            "last30": {"date_from": anchor - timedelta(days=29), "date_to": anchor},
        },
    }


def status_counts(cells) -> dict:
    counts = {status.value.lower(): 0 for status in engine.Status}
    counts["pending"] = 0
    for cell in cells:
        if cell.status:
            counts[str(cell.status).lower()] += 1
        elif cell.fill != engine.Fill.NOT_EMPLOYED:
            counts["pending"] += 1
    counts["total"] = sum(1 for cell in cells if cell.fill != engine.Fill.NOT_EMPLOYED)
    counts["present_days"] = counts["present"] + counts["late"] + counts["half_day"]
    return counts
