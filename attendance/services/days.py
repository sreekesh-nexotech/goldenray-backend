"""Reads of processed days and raw punches: list querysets, where a day's punches came from, the day timeline.

Where a day came from is read back from the raw punches it was built from (``source_raw_ids``), never from the one
``first_device`` column (eSSL §I.17): every contributing terminal, its office and the transports, in one batched
query per page. Raw punches are identified outside the service layer by their ``dedup_key``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.db.models import Q

from attendance.models import AttendanceDay, RawPunch
from attendance.services import inputs
from attendance.services.common import office_timezone
from core.errors import NotFound
from devices.models import Device, DeviceUser
from devices.services import health
from engines import attendance as engine

UNKNOWN = "Unknown"


def days_queryset():
    return AttendanceDay.objects.select_related("employee", "employee__office", "office", "shift", "first_device")


def corrections_queryset():
    from attendance.models import AttendanceCorrection

    return AttendanceCorrection.objects.select_related("day", "day__employee", "created_by", "revoked_by")


def device_label(device) -> str:
    return health.display_label(device) if device is not None else UNKNOWN


def origins(days) -> dict[int, dict]:
    """``day pk → {"sources", "devices", "offices"}`` for days built from punches (one query for the whole page)."""
    wanted: dict[int, list[int]] = {day.pk: [int(value) for value in (day.source_raw_ids or [])] for day in days}
    ids = {raw_id for values in wanted.values() for raw_id in values}
    if not ids:
        return {}
    rows = {raw_id: (source, device) for raw_id, source, device in _raw_rows(ids)}
    result = {}
    for pk, raw_ids in wanted.items():
        if not raw_ids:
            continue
        sources, devices, offices = set(), set(), set()
        for raw_id in raw_ids:
            found = rows.get(raw_id)
            if found is None:
                continue
            source, device = found
            sources.add(source)
            devices.add(device_label(device))
            if device is not None and device.office_id:
                offices.add(device.office.name)
        result[pk] = {"sources": sorted(sources) or [UNKNOWN], "devices": sorted(devices) or [UNKNOWN], "offices": sorted(offices) or [UNKNOWN]}
    return result


def _raw_rows(ids):
    punches = list(RawPunch.objects.filter(id__in=list(ids)).order_by().values_list("id", "source", "device_id"))
    devices = Device.all_objects.select_related("office").in_bulk({device_id for _, _, device_id in punches})
    return [(raw_id, source, devices.get(device_id)) for raw_id, source, device_id in punches]


def raw_queryset():
    return RawPunch.objects.select_related("device", "device__office")


def filter_raw(queryset, *, device=None, pin=None, employee=None, date_from: date | None = None, date_to: date | None = None):
    """``device`` uid, ``pin``, ``employee`` uid (its per-device links, A1), terminal-local ``date_from``/``date_to``."""
    if device is not None:
        queryset = queryset.filter(device__uid=device)
    if pin:
        queryset = queryset.filter(pin=str(pin).strip())
    if employee is not None:
        pairs = list(DeviceUser.objects.filter(employee__uid=employee).values_list("device_id", "pin"))
        condition = inputs.pin_filter(pairs)
        queryset = queryset.filter(condition) if condition is not None else queryset.none()
    if date_from is not None:
        queryset = queryset.filter(device_time__gte=datetime.combine(date_from, time.min))
    if date_to is not None:
        queryset = queryset.filter(device_time__lt=datetime.combine(date_to + timedelta(days=1), time.min))
    return queryset


def people_by_pin(punches) -> dict[tuple[int, str], object]:
    """``(device, pin) → employee`` for a page of punches (one query)."""
    condition = inputs.pin_filter((punch.device_id, punch.pin) for punch in punches)
    if condition is None:
        return {}
    rows = DeviceUser.objects.filter(condition, employee__isnull=False).select_related("employee")
    return {(row.device_id, row.pin): row.employee for row in rows}


def timeline(employee, work_date: date) -> dict:
    """The processed day plus every punch of the person's linked PINs from the day before to the day after.

    Each punch says whether it made the day (``accepted``), was a double scan (``ignored``, A3), or belongs to another
    work date (``in_day: false``).
    """
    links = inputs.links([employee.pk])
    punches = []
    condition = inputs.pin_filter((link.device, link.pin) for link in links)
    zone_name = office_timezone(employee.office)
    if condition is not None:
        low, high = inputs.device_time_window(work_date, work_date)
        punches = list(raw_queryset().filter(condition, device_time__gte=low, device_time__lt=high).order_by("punch_at", "id"))
        start = datetime.combine(work_date - timedelta(days=1), time.min)
        end = datetime.combine(work_date + timedelta(days=2), time.min)
        punches = [punch for punch in punches if start <= engine.local_wall_clock(punch.punch_at, zone_name) < end]
    day = days_queryset().filter(employee=employee, work_date=work_date).first()
    accepted = set(day.source_raw_ids or []) if day is not None else set()
    ignored = set(day.ignored_raw_ids or []) if day is not None else set()
    shift = inputs.engine_shift(inputs.effective_shift(employee))
    rows = []
    for punch in punches:
        local = engine.local_wall_clock(punch.punch_at, zone_name)
        rows.append(
            {
                "punch": punch,
                "office_time": local,
                "work_date": engine.attribute_work_date(local, shift),
                "accepted": punch.id in accepted,
                "ignored": punch.id in ignored,
                "in_day": punch.id in accepted or punch.id in ignored,
            }
        )
    return {"employee": employee, "work_date": work_date, "timezone": zone_name, "day": day, "origin": origins([day]).get(day.pk) if day else None, "punches": rows}


def employee_or_404(queryset, uid):
    employee = queryset.filter(uid=uid).select_related("office", "shift", "office__default_shift").first()
    if employee is None:
        raise NotFound("not_found", "Employee not found.")
    return employee


def search_employees(queryset, search: str | None):
    if search:
        term = search.strip()
        queryset = queryset.filter(Q(full_name__icontains=term) | Q(code__icontains=term))
    return queryset
