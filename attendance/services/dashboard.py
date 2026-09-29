"""``attendance/dashboard/`` (summary, recent punches, trend) and what attendance adds to other screens.

Scoped (eSSL §I.1: its dashboard showed everyone's punches and every device's address to any login): every figure
covers the caller's ``attendance`` scope — ``self`` their own day, ``office`` their office, ``all`` everyone; the device
block appears only for callers who may view devices. Days come from the one calendar fill, today from the punches
received so far (provisional, never stored, A7); "today" is the office's (A10).

Providers installed by ``AttendanceConfig.ready``: hr's ``office_summary["attendance"]`` (the office's day counts, for
callers who may view that office's attendance), ``employee_dependencies["attendance"]`` (days and raw punches: an
employee with either cannot be deleted), and the ``attendance`` counters of ``GET dashboard/``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from accounts.services.authz import can
from attendance.models import AttendanceCorrection, AttendanceDay, RawPunch
from attendance.services import calendar, days, inputs, scopes
from attendance.services.common import now, today_for
from core.errors import DomainError
from devices.models import DeviceUser
from hr.models import Office
from hr.services.scopes import own_employee

MAX_TREND_DAYS = 90
MAX_RECENT = 200


def scoped_employees(user, *, office=None, include_inactive: bool = False):
    queryset = scopes.employees_in_scope(user, inputs.employees_queryset())
    if not include_inactive:
        queryset = queryset.filter(is_active=True)
    if office is not None:
        queryset = queryset.filter(office=office)
    return queryset.order_by("full_name", "pk")


def reference_office(user, office=None):
    """Whose "today": the office asked for, else the caller's own office, else the platform zone."""
    if office is not None:
        return office
    employee = own_employee(user)
    return employee.office if employee is not None else None


def summary(user, *, day: date | None = None, office=None) -> dict:
    at = now()
    target = day or today_for(reference_office(user, office), at=at)
    employees = list(scoped_employees(user, office=office))
    cells, _ = calendar.day_cells(employees, target, at=at)
    offices = {}
    for employee in employees:
        if employee.office_id:
            offices.setdefault(employee.office_id, employee.office)
    punches = _punches_on(user, target, office=office)
    blocks = []
    for office_row in sorted(offices.values(), key=lambda item: item.name):
        members = [cells[employee.pk] for employee in employees if employee.office_id == office_row.pk]
        block = {"office": office_row, "counts": calendar.status_counts(members), "punches": punches.get(office_row.pk, 0), "today": today_for(office_row, at=at)}
        if can(user, "devices", "view"):
            from devices.services.providers import office_summary

            block["devices"] = office_summary(office_row, target, user)
        blocks.append(block)
    all_cells = list(cells.values())
    return {
        "date": target,
        "scope": scopes.scope_of(user),
        "overall": {**calendar.status_counts(all_cells), "currently_in": sum(1 for cell in all_cells if cell.missing_out and cell.fill == calendar.PROVISIONAL), "punches": sum(punches.values())},
        "offices": blocks,
        "provisional": any(cell.fill == calendar.PROVISIONAL for cell in all_cells),
    }


def _punches_on(user, day: date, *, office=None) -> dict:
    """Raw punches of ``day`` (terminal wall clock) within scope, per office of the terminal."""
    queryset = scopes.scoped(RawPunch.objects.filter(device_time__gte=datetime.combine(day, time.min), device_time__lt=datetime.combine(day + timedelta(days=1), time.min)), user)
    if office is not None:
        queryset = queryset.filter(device__office=office)
    counts: dict = {}
    for office_id in queryset.order_by().values_list("device__office_id", flat=True):
        counts[office_id] = counts.get(office_id, 0) + 1
    return counts


def recent_punches(user, *, limit: int = 20, office=None) -> list[dict]:
    if not 1 <= limit <= MAX_RECENT:
        raise DomainError("validation_error", f"limit is 1–{MAX_RECENT}.", errors={"limit": [f"Between 1 and {MAX_RECENT}."]})
    queryset = scopes.scoped(days.raw_queryset(), user)
    if office is not None:
        queryset = queryset.filter(device__office=office)
    rows = list(queryset.order_by("-punch_at", "-id")[:limit])
    people = days.people_by_pin(rows)
    return [{"punch": row, "employee": people.get((row.device_id, row.pin))} for row in rows]


def trend(user, *, days_back: int = 7, office=None) -> list[dict]:
    if not 1 <= days_back <= MAX_TREND_DAYS:
        raise DomainError("validation_error", f"days is 1–{MAX_TREND_DAYS}.", errors={"days": [f"Between 1 and {MAX_TREND_DAYS}."]})
    at = now()
    end = today_for(reference_office(user, office), at=at)
    start = end - timedelta(days=days_back - 1)
    employees = list(scoped_employees(user, office=office))
    filled = calendar.fill(employees, start, end, at=at)
    result = []
    for offset in range((end - start).days + 1):
        cells = [filled[employee.pk][offset] for employee in employees]
        result.append({"date": start + timedelta(days=offset), **calendar.status_counts(cells)})
    return result


# --------------------------------------------------------------------------------------------------------------------
# providers for other screens
# --------------------------------------------------------------------------------------------------------------------
def office_summary(office, day, user) -> dict | None:
    """hr ``GET hr/offices/<uid>/summary/`` section: the office's attendance for ``day`` (only within the caller's scope)."""
    if not can(user, "attendance", "view"):
        return None
    employees = list(scoped_employees(user, office=office))
    if not employees:
        return None
    cells, _ = calendar.day_cells(employees, day or today_for(office))
    return {"date": (day or today_for(office)).isoformat(), **calendar.status_counts(cells.values())}


def employee_dependencies(employee) -> dict:
    pairs = list(DeviceUser.objects.filter(employee=employee).values_list("device_id", "pin"))
    condition = inputs.pin_filter(pairs)
    raw = RawPunch.objects.filter(condition).count() if condition is not None else 0
    return {"attendance_days": AttendanceDay.objects.filter(employee=employee).count(), "raw_punches": raw}


def dashboard_counters(user) -> dict[str, int]:
    """``GET dashboard/`` → ``attendance``: yesterday's final days (platform zone) and open corrections, in scope."""
    yesterday = today_for(None) - timedelta(days=1)
    stored = scopes.scoped(AttendanceDay.objects.filter(work_date=yesterday), user)
    counts = {status: 0 for status in ("PRESENT", "LATE", "ABSENT", "HALF_DAY", "ON_LEAVE")}
    for status in stored.order_by().values_list("status", flat=True):
        if status in counts:
            counts[status] += 1
    return {
        "present_yesterday": counts["PRESENT"] + counts["LATE"],
        "late_yesterday": counts["LATE"],
        "half_day_yesterday": counts["HALF_DAY"],
        "absent_yesterday": counts["ABSENT"],
        "on_leave_yesterday": counts["ON_LEAVE"],
        "active_corrections": scopes.scoped(AttendanceCorrection.objects.filter(revoked_at__isnull=True), user).count(),
    }


def install() -> None:
    from core import dashboard
    from hr import registries

    registries.office_summary.register("attendance")(office_summary)
    registries.employee_dependencies.register("attendance")(employee_dependencies)
    dashboard.register("attendance")(dashboard_counters)


def office_or_none(uid):
    if uid is None:
        return None
    office = Office.objects.filter(uid=uid).first()
    if office is None:
        raise DomainError("validation_error", "Unknown office.", errors={"office": ["No such office."]})
    return office
