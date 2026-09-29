"""Where accepted punches go: the interface between devices (transport) and attendance (raw punch storage).

The devices package receives punches (agent uploads, ADMS ATTLOG pushes), validates and normalises them and computes
their content dedup key (A11); **storing** them in ``attendance_raw_punch`` belongs to the attendance package, which
registers a sink at startup::

    from devices.services import punch_sink

    class RawPunchSink(punch_sink.PunchSink):
        def store(self, punches): ...          # insert ON CONFLICT (dedup_key) DO NOTHING; report what was new
        def status(self, device): ...          # highest device record uid, newest device time, stored count
        def pin_activity(self, device): ...    # per PIN of one device: punches, first, last
        def observed_codes(self): ...          # distinct raw status/punch codes with counts

    punch_sink.register(RawPunchSink())

:func:`store` is called inside the ingesting service's transaction, so a punch is stored together with its sync log
and its ``attendance.punches_ingested`` event, or not at all. Until a sink is registered the default
:class:`NullSink` stores nothing and reports every valid punch as ``discarded`` (never as new), so an agent's upload
result and the sync log say truthfully that nothing was kept.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Punch:
    """One accepted punch, normalised (``device_time`` is naive: the terminal's wall clock, as reported)."""

    device_id: int
    device_uid: str
    device_serial: str
    office_timezone: str
    pin: str
    device_time: datetime
    status_code: int | None
    punch_code: int | None
    source: str  # AGENT_PUSH | ADMS_PUSH | IMPORT
    dedup_key: str
    device_record_uid: int | None = None
    agent_id: int | None = None
    adms_request_id: int | None = None
    raw_payload: dict = field(default_factory=dict)
    received_at: datetime | None = None


@dataclass
class SinkResult:
    received: int = 0
    new: int = 0
    duplicate: int = 0
    discarded: int = 0
    new_punches: list[Punch] = field(default_factory=list)


def dedup_key(serial: str, pin: str, device_time: datetime, status_code: int | None, punch_code: int | None) -> str:
    """``sha256(serial|pin|device_time|status|punch)`` — the same key whichever transport delivered the punch (A11)."""
    parts = [serial or "", pin, device_time.replace(tzinfo=None, microsecond=0).isoformat(), "" if status_code is None else str(status_code), "" if punch_code is None else str(punch_code)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class PunchSink:
    """The interface (and the behaviour of :class:`NullSink`)."""

    installed = False

    def store(self, punches: list[Punch]) -> SinkResult:
        return SinkResult(received=len(punches), discarded=len(punches))

    def status(self, device) -> dict:
        return {"stored_records": None, "highest_device_record_uid": None, "latest_device_time": None}

    def pin_activity(self, device) -> list[dict]:
        """``[{"pin", "punch_count", "first_punch_at", "last_punch_at"}]`` for one device (device wall-clock times)."""
        return []

    def observed_codes(self) -> list[dict]:
        """``[{"field": "status"|"punch", "raw_value", "count", "device_platform", "firmware_version"}]``."""
        return []


class NullSink(PunchSink):
    """Stores nothing (no attendance package installed)."""


_sink: PunchSink = NullSink()


def register(sink: PunchSink) -> PunchSink:
    global _sink
    _sink = sink
    return sink


def reset() -> None:
    register(NullSink())


def get() -> PunchSink:
    return _sink


def installed() -> bool:
    return bool(getattr(_sink, "installed", False))
