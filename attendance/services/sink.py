"""The punch store behind ``devices.services.punch_sink`` (DV-78): agent uploads and ADMS pushes land here.

``devices`` validates a batch, computes each punch's content dedup key (A11) and calls :meth:`RawPunchSink.store`
inside its own transaction; this module appends the punches to ``attendance_raw_punch`` with
``INSERT … ON CONFLICT (dedup_key, device_time) DO NOTHING RETURNING`` — so a resend, or the same punch arriving by the
other transport, is a duplicate and never a second row — and reports exactly which punches were new (devices then
publishes ``attendance.punches_ingested`` for those only; the recompute runs later, in Celery, A8).

``punch_at`` is the terminal's wall clock read in its office's zone (A10); the clock offset is never applied.
"""

from __future__ import annotations

import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.db.models import Count, Max, Min

from attendance.models import RawPunch
from devices.services import punch_sink
from engines import attendance as engine

COLUMNS = (
    "device_id",
    "device_serial",
    "device_record_uid",
    "pin",
    "device_time",
    "punch_at",
    "status_code",
    "punch_code",
    "source",
    "agent_id",
    "adms_request_id",
    "dedup_key",
    "raw_payload",
    "received_at",
)
CHUNK = 500
SOURCES = set(RawPunch.Source.values)


def punch_at(device_time, timezone_name: str):
    zone_name = timezone_name if engine.is_valid_timezone(timezone_name) else engine.DEFAULT_TIMEZONE
    return engine.punch_at_from_device_time(device_time.replace(tzinfo=None), zone_name)


def _row(punch: punch_sink.Punch) -> tuple:
    source = punch.source if punch.source in SOURCES else RawPunch.Source.IMPORT
    return (
        punch.device_id,
        (punch.device_serial or "")[:80],
        punch.device_record_uid,
        punch.pin,
        punch.device_time.replace(tzinfo=None),
        punch_at(punch.device_time, punch.office_timezone),
        punch.status_code,
        punch.punch_code,
        source,
        punch.agent_id,
        punch.adms_request_id,
        punch.dedup_key,
        json.dumps(punch.raw_payload or {}, cls=DjangoJSONEncoder),
        punch.received_at,
    )


def insert(punches: list[punch_sink.Punch]) -> set[str]:
    """Append the punches that are not stored yet; returns the dedup keys of the rows written."""
    written: set[str] = set()
    placeholders = "(" + ", ".join("%s::jsonb" if name == "raw_payload" else ("COALESCE(%s, now())" if name == "received_at" else "%s") for name in COLUMNS) + ")"
    with connection.cursor() as cursor:
        for start in range(0, len(punches), CHUNK):
            chunk = punches[start : start + CHUNK]
            params: list = []
            for punch in chunk:
                params.extend(_row(punch))
            cursor.execute(
                f"INSERT INTO attendance_raw_punch ({', '.join(COLUMNS)}) VALUES {', '.join([placeholders] * len(chunk))} " "ON CONFLICT (dedup_key, device_time) DO NOTHING RETURNING dedup_key",
                params,
            )
            written.update(row[0] for row in cursor.fetchall())
    return written


class RawPunchSink(punch_sink.PunchSink):
    installed = True

    def store(self, punches):
        result = punch_sink.SinkResult(received=len(punches))
        if not punches:
            return result
        written = insert(list(punches))
        for punch in punches:
            if punch.dedup_key in written:
                result.new += 1
                result.new_punches.append(punch)
            else:
                result.duplicate += 1
        return result

    def status(self, device) -> dict:
        data = RawPunch.objects.filter(device_id=device.pk).aggregate(count=Count("id"), uid=Max("device_record_uid"), latest=Max("device_time"))
        return {"stored_records": data["count"], "highest_device_record_uid": data["uid"], "latest_device_time": data["latest"]}

    def pin_activity(self, device) -> list[dict]:
        rows = RawPunch.objects.filter(device_id=device.pk).order_by().values("pin").annotate(punch_count=Count("id"), first_punch_at=Min("device_time"), last_punch_at=Max("device_time"))
        return [dict(row) for row in rows.order_by("pin")]

    def observed_codes(self) -> list[dict]:
        found = []
        for field, column in (("status", "status_code"), ("punch", "punch_code")):
            rows = RawPunch.objects.order_by().values(column, "device__platform", "device__firmware_version").annotate(count=Count("id"))
            for row in rows:
                found.append(
                    {"field": field, "raw_value": row[column], "count": row["count"], "device_platform": row["device__platform"] or "", "firmware_version": row["device__firmware_version"] or ""}
                )
        return found


def install() -> punch_sink.PunchSink:
    return punch_sink.register(RawPunchSink())
