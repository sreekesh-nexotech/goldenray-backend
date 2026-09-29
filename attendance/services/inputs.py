"""Engine inputs read from the database (``engines.attendance`` is pure: every row it needs is handed to it here).

One place builds ``Shift``/``Employee``/``Holiday``/``Leave``/``AttendanceRule``/``DeviceLink``/``RawPunch`` from the
``hr_*``, ``devices_device_user`` and ``attendance_raw_punch`` rows, so the recompute, the calendars, the reports and
the dashboards all see the same people, shifts and calendar.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from django.db.models import Q

from attendance.models import AttendanceDay, RawPunch
from attendance.services.common import office_timezone
from devices.models import DeviceUser
from engines import attendance as engine
from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord

SHIFT_FIELDS = (
    "is_overnight",
    "overnight_buffer_minutes",
    "grace_minutes",
    "late_threshold_minutes",
    "early_exit_threshold_minutes",
    "full_day_minutes",
    "half_day_minutes",
    "half_day_after_minutes",
    "break_minutes",
    "auto_deduct_break",
    "debounce_minutes",
    "overtime_enabled",
    "overtime_after_minutes",
)


def employees_queryset():
    """Live employees with what the engine needs (own shift, office, office default shift) in one query."""
    return Employee.objects.select_related("shift", "office", "office__default_shift")


def effective_shift(employee):
    """The ``hr_shift`` row that applies: the employee's own, else the office default, else none."""
    return employee.effective_shift


def engine_shift(shift) -> engine.Shift | None:
    if shift is None:
        return None
    values = {name: getattr(shift, name) for name in SHIFT_FIELDS}
    return engine.Shift(
        start_time=shift.start_time,
        end_time=shift.end_time,
        key=shift.pk,
        working_days=tuple(int(day) for day in (shift.working_days or ())),
        weekly_off_days=tuple(int(day) for day in (shift.weekly_off_days or ())),
        **values,
    )


def engine_employee(employee, shifts: dict | None = None) -> engine.Employee:
    shift = effective_shift(employee)
    cache = shifts if shifts is not None else {}
    if shift is not None and shift.pk not in cache:
        cache[shift.pk] = engine_shift(shift)
    return engine.Employee(
        key=employee.pk,
        office=employee.office_id,
        timezone=office_timezone(employee.office),
        shift=cache.get(shift.pk) if shift is not None else None,
        joined_on=employee.joined_on,
        left_on=employee.left_on,
    )


def engine_employees(employees: Iterable) -> list[engine.Employee]:
    shifts: dict = {}
    return [engine_employee(employee, shifts) for employee in employees]


def holidays(date_from: date, date_to: date) -> list[engine.Holiday]:
    rows = Holiday.objects.filter(date__gte=date_from, date__lte=date_to, is_active=True).values_list("date", "name", "office_id")
    return [engine.Holiday(date=day, name=name, office=office_id) for day, name, office_id in rows]


def leaves(employee_ids: Iterable[int], date_from: date, date_to: date) -> list[engine.Leave]:
    rows = (
        LeaveRecord.objects.filter(employee_id__in=list(employee_ids), status=LeaveRecord.Status.APPROVED, date_from__lte=date_to, date_to__gte=date_from)
        .select_related("leave_type")
        .order_by("date_from", "id")
    )
    return [
        engine.Leave(
            employee=row.employee_id, date_from=row.date_from, date_to=row.date_to, status=row.status, is_half_day=row.is_half_day, leave_type=row.leave_type.name if row.leave_type_id else None
        )
        for row in rows
    ]


def rules() -> list[engine.AttendanceRule]:
    rows = AttendanceRule.objects.filter(is_active=True).values_list("rules", "office_id", "shift_id", "effective_from", "pk")
    return [engine.AttendanceRule(rules=payload or {}, office=office_id, shift=shift_id, effective_from=effective_from, key=pk) for payload, office_id, shift_id, effective_from, pk in rows]


def links(employee_ids: Iterable[int] | None = None) -> list[engine.DeviceLink]:
    """Per-device PIN links (A1) of live device users on live devices."""
    queryset = DeviceUser.objects.filter(employee__isnull=False, device__deleted_at__isnull=True)
    if employee_ids is not None:
        queryset = queryset.filter(employee_id__in=list(employee_ids))
    return [engine.DeviceLink(device=device_id, pin=pin, employee=employee_id) for device_id, pin, employee_id in queryset.values_list("device_id", "pin", "employee_id")]


def device_time_window(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """Terminal wall-clock bounds covering every punch that can belong to ``[date_from, date_to]``.

    The engine wants punches from the day before (overnight shifts) to the day after; a terminal's clock is its office's
    wall clock, so one more day on each side absorbs any zone between the terminal and the employee's office.
    """
    return datetime.combine(date_from - timedelta(days=2), time.min), datetime.combine(date_to + timedelta(days=3), time.min)


def pin_filter(pairs: Iterable[tuple[int, str]]) -> Q | None:
    by_device: dict[int, set[str]] = defaultdict(set)
    for device_id, pin in pairs:
        by_device[device_id].add(pin)
    condition = None
    for device_id, pins in by_device.items():
        clause = Q(device_id=device_id, pin__in=sorted(pins))
        condition = clause if condition is None else condition | clause
    return condition


def raw_punches(device_links: Iterable[engine.DeviceLink], date_from: date, date_to: date) -> list[engine.RawPunch]:
    condition = pin_filter((link.device, link.pin) for link in device_links)
    if condition is None:
        return []
    low, high = device_time_window(date_from, date_to)
    rows = RawPunch.objects.filter(condition, device_time__gte=low, device_time__lt=high).order_by().values_list("id", "device_id", "pin", "punch_at")
    return [engine.RawPunch(raw_id=raw_id, device=device_id, pin=pin, punch_at=punch_at) for raw_id, device_id, pin, punch_at in rows]


def corrected_days(employee_ids: Iterable[int], date_from: date, date_to: date) -> set[tuple[int, date]]:
    rows = AttendanceDay.objects.filter(employee_id__in=list(employee_ids), work_date__gte=date_from, work_date__lte=date_to, is_corrected=True).values_list("employee_id", "work_date")
    return set(rows)


def calendars(employee_ids: Iterable[int], date_from: date, date_to: date) -> tuple[engine.HolidayCalendar, engine.LeaveBook]:
    return engine.HolidayCalendar(holidays(date_from, date_to)), engine.LeaveBook(leaves(employee_ids, date_from, date_to))
