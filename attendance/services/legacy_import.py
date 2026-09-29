"""eSSL import (PLAN §7.5): raw punches with new content keys, and the v3 → v4 status diff for HR sign-off.

``hr.services.legacy_import.import_all`` (offices, shifts, employees, holidays, leave) and
``devices.services.legacy_import.import_all`` (agents, devices, per-device PIN links) run first; both functions here
take plain ``SELECT *`` row dicts of the eSSL tables and return ``{"created", "updated", "skipped", "violations"}``
(plus their own report keys), write one ``attendance.legacy_imported`` audit row per call (counts + sha256 of the
batch) and are idempotent through ``core_legacy_map`` (``ESSL`` × table × id): a re-run creates nothing twice.

* :func:`import_raw_punches` — ``attendance_raw`` → ``attendance_raw_punch`` (source ``IMPORT``). Every punch gets the
  platform's content key (A11, the key the agent and the ADMS receiver compute —
  ``devices.services.punch_sink.dedup_key``), so the same punch stored twice by eSSL (an ADMS push and the agent's
  upload of it, eSSL §I.22) **collapses** to one row: the second source row is mapped to the surviving punch and
  listed under ``collapsed``; a punch the agent delivers again after the cutover is a duplicate, never a new row. The
  eSSL key, source, agent and row id are kept in ``raw_payload._essl``. Rows of devices that were not imported, without
  a PIN or with an unreadable time are skipped and listed; codes beyond the columns (smallint) are kept in the payload
  only (like the live receiver).
* :func:`status_diff_report` — eSSL ``attendance`` (v3 stored days) is **not** imported: the covered range is
  recomputed by engine v4 from the imported punches, holidays and leave, and the v3 status of every stored day is
  compared with v4's, per employee and month, for HR to sign off (PLAN §7.5, §7.6 #11). A day v3 never stored (a
  weekly off without punches: eSSL filled it when read) is compared with what v3 would have shown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from attendance.models import AttendanceDay, RawPunch
from attendance.services import partitions, recompute, sink
from attendance.services.common import bump_days
from audit.services import record
from core.models import LegacyMap
from devices.models import Device
from devices.services import punch_sink
from devices.services.common import INT_RANGE, SMALLINT_RANGE, fits, scrub_deep
from engines import attendance as engine
from hr.models import Employee
from hr.services.legacy_import import ESSL, Report, checksum, mapped, mapped_id

RAW_TABLE = "attendance_raw"
DAY_TABLE = "attendance"
IMPORT_SOURCE = RawPunch.Source.IMPORT


def _device_time(value) -> datetime | None:
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        return None
    return parsed.replace(tzinfo=None, microsecond=0)  # eSSL kept the terminal's wall clock, naive


def _code(value) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _audit(action_table: str, rows: list[dict], report: dict, user) -> None:
    record(
        "attendance.legacy_imported",
        object_type="attendance.rawpunch" if action_table == RAW_TABLE else "attendance.attendanceday",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={
            "source": f"ESSL {action_table}",
            "rows": len(rows),
            "checksum": checksum(rows),
            **{key: (len(value) if isinstance(value, list) else value) for key, value in report.items() if key in ("created", "updated", "skipped", "violations", "collapsed")},
        },
    )


# --------------------------------------------------------------------------------------------------------------------
# attendance_raw → attendance_raw_punch
# --------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def import_raw_punches(rows: Iterable[dict], *, user=None) -> dict:
    rows = sorted(rows, key=lambda row: int(row.get("id") or 0))
    report = Report()
    collapsed: list[dict] = []
    already = set(LegacyMap.objects.filter(source_system=ESSL, source_table=RAW_TABLE, source_id__in=[str(row.get("id")) for row in rows]).values_list("source_id", flat=True))
    devices: dict = {}
    pending: list[tuple[dict, punch_sink.Punch]] = []
    for row in rows:
        source_id = row.get("id")
        if str(source_id) in already:
            report.skipped += 1
            continue
        if row.get("device_id") not in devices:
            devices[row.get("device_id")] = mapped(Device, "devices", row.get("device_id"))
        device = devices[row.get("device_id")]
        if device is None:
            report.violation(source_id, "device_id", f"device {row.get('device_id')} was not imported; punch skipped")
            continue
        pin = str(row.get("device_user_id") or "").strip()
        device_time = _device_time(row.get("punch_time"))
        if not pin or len(pin) > 80 or device_time is None:
            report.violation(source_id, "row", "a punch needs a PIN (≤ 80 characters) and a readable time; skipped")
            continue
        status, punch = _code(row.get("status")), _code(row.get("punch"))
        payload = scrub_deep(dict(row.get("raw_payload") or {}))
        payload["_essl"] = {"id": source_id, "dedup_key": row.get("dedup_key"), "source": row.get("source"), "agent_id": row.get("agent_id"), "status": row.get("status"), "punch": row.get("punch")}
        if not (fits(status, SMALLINT_RANGE) and fits(punch, SMALLINT_RANGE)):
            report.violation(source_id, "status", "a raw code beyond smallint is kept in raw_payload only")
            status = status if fits(status, SMALLINT_RANGE) else None
            punch = punch if fits(punch, SMALLINT_RANGE) else None
        record_uid = _code(row.get("device_record_uid"))
        serial = device.serial_number or device.expected_serial or str(row.get("device_serial") or "")
        agent_id = mapped_id("agents", row.get("agent_id"))
        pending.append(
            (
                row,
                punch_sink.Punch(
                    device_id=device.pk,
                    device_uid=str(device.uid),
                    device_serial=serial,
                    office_timezone=device.office.timezone if device.office_id else engine.DEFAULT_TIMEZONE,
                    pin=pin,
                    device_time=device_time,
                    status_code=status,
                    punch_code=punch,
                    source=IMPORT_SOURCE,
                    dedup_key=punch_sink.dedup_key(serial, pin, device_time, status, punch),
                    device_record_uid=record_uid if fits(record_uid, INT_RANGE) else None,
                    agent_id=agent_id,
                    raw_payload=payload,
                    received_at=parse_datetime(row["received_at"]) if isinstance(row.get("received_at"), str) else (row.get("received_at") or timezone.now()),
                ),
            )
        )
    if pending and partitions.connected_as_owner():
        partitions.ensure_months([item.device_time.date() for _, item in pending])
    unique: dict[str, punch_sink.Punch] = {}
    for _, item in pending:
        unique.setdefault(item.dedup_key, item)
    written = sink.insert(list(unique.values())) if unique else set()
    ids = dict(RawPunch.objects.filter(dedup_key__in=list(unique)).values_list("dedup_key", "id")) if unique else {}
    first_source: dict[str, object] = {}
    for row, item in pending:
        source_id = row.get("id")
        LegacyMap.objects.create(source_system=ESSL, source_table=RAW_TABLE, source_id=str(source_id), target_table=RawPunch._meta.db_table, target_id=ids[item.dedup_key])
        if item.dedup_key in written and item.dedup_key not in first_source:
            first_source[item.dedup_key] = source_id
            report.created += 1
            continue
        report.skipped += 1
        collapsed.append(
            {
                "source_id": str(source_id),
                "kept": str(first_source.get(item.dedup_key, "stored before")),
                "pin": item.pin,
                "device_time": item.device_time.isoformat(),
                "essl_source": row.get("source"),
                "reason": "same serial, PIN, time and codes as a punch already stored (A11)",
            }
        )
    result = {**report.as_dict(), "collapsed": collapsed}
    _audit(RAW_TABLE, rows, result, user)
    return result


# --------------------------------------------------------------------------------------------------------------------
# attendance (v3) → recompute (v4) + the per-employee-month status diff
# --------------------------------------------------------------------------------------------------------------------
def _v3_unstored_status(employee: engine.Employee, day: date, holidays, leaves) -> str:
    """What eSSL's calendar showed for a day it never stored (leave > holiday > weekly off > absent, C10)."""
    context = engine.day_context(employee.key, employee.office, day, holidays, leaves)
    if context.on_leave:
        return "ON_LEAVE"
    if context.is_holiday:
        return "HOLIDAY"
    return "WEEKLY_OFF" if not engine.is_working_day(day, employee.shift) else "ABSENT"


def status_diff_report(rows: Iterable[dict], *, user=None, at: datetime | None = None) -> dict:
    """Recompute the range eSSL's ``attendance`` rows cover (v4) and compare the statuses per employee and month."""
    from attendance.services import inputs

    rows = list(rows)
    report = Report()
    v3: dict[tuple[int, date], dict] = {}
    employees: dict[object, Employee | None] = {}
    for row in rows:
        source = row.get("employee_id")
        if source not in employees:
            employees[source] = mapped(Employee, "employees", source)
        employee = employees[source]
        work_date = parse_date(row["work_date"]) if isinstance(row.get("work_date"), str) else row.get("work_date")
        if employee is None or work_date is None:
            report.violation(row.get("id"), "employee_id", f"employee {source} was not imported; day not compared")
            continue
        if row.get("is_manual_override"):
            report.violation(row.get("id"), "is_manual_override", "a manual override (no eSSL API could set one) is not carried over; the day is recomputed")
        v3[(employee.pk, work_date)] = row
    people = sorted({employee for employee in employees.values() if employee is not None}, key=lambda item: item.pk)
    if not v3:
        result = {**report.as_dict(), "skipped": len(rows), "months": [], "days_compared": 0, "days_differing": 0}
        _audit(DAY_TABLE, rows, result, user)
        return result
    first, last = min(day for _, day in v3), max(day for _, day in v3)
    summary = recompute.recompute(date_from=first, date_to=last, employee_ids=[person.pk for person in people], reason="essl_import", user=user, at=at)
    report.created, report.updated = summary["created"], summary["updated"]
    report.skipped = len(rows) - len(v3)
    v4 = {(day.employee_id, day.work_date): day for day in AttendanceDay.objects.filter(employee__in=people, work_date__gte=first, work_date__lte=last)}
    staff = {person.key: person for person in inputs.engine_employees(people)}
    holidays, leaves = inputs.calendars([person.pk for person in people], first, last)
    months: dict[tuple[int, str], dict] = {}
    compared = differing = 0
    day = first
    while day <= last:
        for person in people:
            old = v3.get((person.pk, day))
            new = v4.get((person.pk, day))
            if old is None and new is None:
                continue
            v3_status = str(old["status"]) if old is not None else _v3_unstored_status(staff[person.pk], day, holidays, leaves)
            v4_status = new.status if new is not None else ""
            key = (person.pk, f"{day:%Y-%m}")
            month = months.setdefault(key, {"employee": person, "month": key[1], "v3": Counter(), "v4": Counter(), "differences": []})
            month["v3"][v3_status] += 1
            month["v4"][v4_status or "NOT_STORED"] += 1
            compared += 1
            if v3_status != v4_status:
                differing += 1
                month["differences"].append(
                    {
                        "date": day.isoformat(),
                        "v3_status": v3_status,
                        "v4_status": v4_status or "NOT_STORED",
                        "v3_stored": old is not None,
                        "v3_working_minutes": int(old.get("working_minutes") or 0) if old is not None else 0,
                        "v4_working_minutes": new.working_minutes if new is not None else 0,
                        "v3_punch_count": int(old.get("punch_count") or 0) if old is not None else 0,
                        "v4_punch_count": new.punch_count if new is not None else 0,
                        "worked_on_off_day": bool(new.worked_on_off_day) if new is not None else False,
                        "leave_conflict": bool(new.leave_conflict) if new is not None else False,
                    }
                )
        day += timedelta(days=1)
    listing = [
        {
            "employee_uid": str(item["employee"].uid),
            "employee_code": item["employee"].code,
            "month": item["month"],
            "v3": dict(sorted(item["v3"].items())),
            "v4": dict(sorted(item["v4"].items())),
            "days_differing": len(item["differences"]),
            "differences": item["differences"],
        }
        for _, item in sorted(months.items(), key=lambda entry: (entry[1]["employee"].code, entry[0][1]))
    ]
    result = {**report.as_dict(), "months": listing, "days_compared": compared, "days_differing": differing, "recompute": summary}
    _audit(DAY_TABLE, rows, result, user)
    bump_days()
    return result


def import_all(tables: dict[str, list[dict]], *, user=None, at: datetime | None = None) -> dict[str, dict]:
    """Raw punches, then the recompute and the diff (after the hr and devices imports)."""
    return {
        RAW_TABLE: import_raw_punches(tables.get(RAW_TABLE, []), user=user),
        DAY_TABLE: status_diff_report(tables.get(DAY_TABLE, []), user=user, at=at),
    }


def month_totals(report: dict) -> dict[str, dict]:
    """``{status: (v3, v4)}`` over every month of a diff report (a sign-off summary)."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for month in report.get("months", []):
        for status, count in month["v3"].items():
            totals[status][0] += count
        for status, count in month["v4"].items():
            totals[status][1] += count
    return {status: {"v3": pair[0], "v4": pair[1]} for status, pair in sorted(totals.items())}
