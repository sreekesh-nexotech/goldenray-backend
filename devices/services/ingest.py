"""Punch ingestion shared by both transports (agent uploads, ADMS ATTLOG pushes).

For one device and one batch: validate and normalise every record (PIN as a string, ``device_time`` naive — the
terminal's wall clock, a zone offset is dropped without converting), compute the content dedup key (A11: the same
key whichever transport delivered it, so a punch read by the agent and pushed over ADMS collapses), hand the valid
punches to the registered :mod:`~devices.services.punch_sink`, write one ATTENDANCE sync log, move the device's
counters, and publish ``attendance.punches_ingested`` for the punches the sink reports as new (A8: the attendance
package recomputes; nothing is computed inside the uploading request).

Event contract (``attendance.punches_ingested``)::

    {"device_uid": "<uuid>", "office_uid": "<uuid>" | null, "source": "AGENT_PUSH" | "ADMS_PUSH",
     "new": <int>, "employee_uids": ["<uuid>", …], "unmapped_pins": ["<pin>", …],
     "dates": ["YYYY-MM-DD", …], "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}

``dates`` are the terminal-local dates of the new punches; PINs resolve per device (A1) through the device-user links.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import F
from django.utils.dateparse import parse_datetime

from core.outbox import emit
from devices.models import Device, DeviceUser, SyncLog
from devices.services import punch_sink
from devices.services.common import now

EVENT = "attendance.punches_ingested"
AGENT_PUSH = "AGENT_PUSH"
ADMS_PUSH = "ADMS_PUSH"
MAX_PIN_LENGTH = 80
EARLIEST_PLAUSIBLE = datetime(2000, 1, 1)
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M")


def office_zone(device: Device) -> zoneinfo.ZoneInfo:
    name = device.office.timezone if device.office_id else settings.TIME_ZONE
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def parse_device_time(value) -> datetime | None:
    """A naive datetime (seconds precision) from a datetime or text; a zone offset is dropped, never converted."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        parsed = None
        for fmt in TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            parsed = parse_datetime(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    else:
        return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None, microsecond=0)


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def localise(device: Device, device_time: datetime) -> datetime:
    """The terminal's wall clock read in the office's zone (A10); used for ``last_punch_at``."""
    return device_time.replace(tzinfo=office_zone(device))


def device_serial(device: Device) -> str:
    return device.serial_number or device.expected_serial or ""


def build_punches(device: Device, records, *, source: str, agent=None, adms_request_id: int | None = None) -> tuple[list[punch_sink.Punch], int, int]:
    """``(punches, invalid, duplicates_in_batch)`` — invalid: no PIN, an unreadable or implausible time."""
    received_at = now()
    latest_plausible = received_at.astimezone(office_zone(device)).replace(tzinfo=None) + timedelta(days=2)
    serial = device_serial(device)
    zone = device.office.timezone if device.office_id else settings.TIME_ZONE
    punches, seen = [], set()
    invalid = duplicates = 0
    for record in records:
        pin = str(record.get("pin") or "").strip()
        device_time = parse_device_time(record.get("device_time"))
        if not pin or len(pin) > MAX_PIN_LENGTH or device_time is None or not (EARLIEST_PLAUSIBLE <= device_time <= latest_plausible):
            invalid += 1
            continue
        status_code, punch_code = _to_int(record.get("status")), _to_int(record.get("punch"))
        key = punch_sink.dedup_key(serial, pin, device_time, status_code, punch_code)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        punches.append(
            punch_sink.Punch(
                device_id=device.pk,
                device_uid=str(device.uid),
                device_serial=serial,
                office_timezone=zone,
                pin=pin,
                device_time=device_time,
                status_code=status_code,
                punch_code=punch_code,
                source=source,
                dedup_key=key,
                device_record_uid=_to_int(record.get("device_record_uid")),
                agent_id=agent.pk if agent is not None else None,
                adms_request_id=adms_request_id,
                raw_payload=dict(record.get("raw_payload") or {}),
                received_at=received_at,
            )
        )
    return punches, invalid, duplicates


def _announce(device: Device, source: str, new_punches: list[punch_sink.Punch]) -> None:
    pins = sorted({punch.pin for punch in new_punches})
    links = dict(DeviceUser.objects.filter(device=device, pin__in=pins, employee__isnull=False).values_list("pin", "employee__uid"))
    dates: list[date] = sorted({punch.device_time.date() for punch in new_punches})
    emit(
        EVENT,
        {
            "device_uid": str(device.uid),
            "office_uid": str(device.office.uid) if device.office_id else None,
            "source": source,
            "new": len(new_punches),
            "employee_uids": sorted({str(uid) for uid in links.values()}),
            "unmapped_pins": [pin for pin in pins if pin not in links],
            "dates": [day.isoformat() for day in dates],
            "date_from": dates[0].isoformat(),
            "date_to": dates[-1].isoformat(),
        },
        aggregate_type="devices.device",
        aggregate_uid=device.uid,
    )


def ingest(device: Device, records, *, source: str, agent=None, adms_request_id: int | None = None, batch_id: str = "") -> dict:
    """Store one batch (inside the caller's transaction). Returns the counts the uploader and the evidence show."""
    started = now()
    records = list(records)
    punches, invalid, in_batch = build_punches(device, records, source=source, agent=agent, adms_request_id=adms_request_id)
    result = punch_sink.get().store(punches) if punches else punch_sink.SinkResult()
    duplicate = result.duplicate + in_batch
    finished = now()
    values = {"last_sync_at": finished}
    if punches:
        newest = localise(device, max(punch.device_time for punch in punches))
        if device.last_punch_at is None or newest > device.last_punch_at:
            values["last_punch_at"] = newest
    Device.all_objects.filter(pk=device.pk).update(attendance_count=F("attendance_count") + result.new, **values)
    for name, value in values.items():
        setattr(device, name, value)
    if records:
        SyncLog.objects.create(
            device=device,
            agent=agent,
            sync_type=SyncLog.Type.ATTENDANCE,
            status=SyncLog.Status.PARTIAL if invalid else SyncLog.Status.SUCCESS,
            started_at=started,
            finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000),
            records_read=len(records),
            records_new=result.new,
            records_duplicate=duplicate,
            error_message=f"{invalid} record(s) without a PIN or a readable, plausible time were skipped." if invalid else "",
            details={
                "transport": source,
                "agent": agent.code if agent is not None else None,
                "batch_id": batch_id or None,
                "invalid": invalid,
                "discarded": result.discarded,
                "punch_store_installed": punch_sink.installed(),
                "adms_request_id": adms_request_id,
            },
        )
    if result.new_punches:
        _announce(device, source, result.new_punches)
    return {"received": len(records), "new": result.new, "duplicate": duplicate, "invalid": invalid, "discarded": result.discarded}
