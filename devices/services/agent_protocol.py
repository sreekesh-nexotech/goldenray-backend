"""The office-agent protocol (``/api/agent/<version>/``, service token; eSSL ``agent_service`` without its defects).

* **Scope.** An agent reports only for devices bound to it (404 ``device_not_found`` otherwise — "announce first").
* **Announce** binds a terminal the agent can read to its device record, resolved by the serial the terminal reports
  (``serial_number``, then the ``expected_serial`` pin; the LAN address only for this agent's own serial-less record).
  It **never re-homes** a device bound to another agent, nor adopts an unbound device registered in another office:
  409 ``device_bound_elsewhere`` (recorded on the device, audited; eSSL let any agent take any terminal, spec §I.5).
  Re-homing is ``POST devices/<uid>/rehome/`` (``devices.manage``). A reported serial that contradicts the pin, or a
  MAC that contradicts the known one, is 409 ``identity_mismatch`` and is recorded (sticky) before the refusal.
* **Health** is derived from timestamps: a heartbeat entry with ``reachable: true`` (the agent really reached the
  terminal on the LAN) moves ``last_seen_at``; ``reachable: false`` records the error and moves nothing (eSSL's agent
  said ``is_online: true`` every 2 s even when the LAN read failed, spec §I.19). Uploads never pretend contact.
* **Users** — a whole-table read: rows are upserted (never deleted) and a USERS sync log records exactly which PINs
  the read found (the presence watermark), PARTIAL when some entries were unreadable.
* **Attendance** — batches of at most 200 punches, idempotent by the content dedup key (A11) and by ``Idempotency-Key``;
  stored through the punch sink, which publishes ``attendance.punches_ingested`` (A8).
* Telemetry (contact times, counters, the hardware's own identity fields) is written without a version bump, so a
  heartbeat never makes a staff member's edit stale; staff-owned configuration is never touched here.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction

from audit.services import record
from core.errors import Conflict, NotFound
from core.services import stamp_create
from devices.models import Agent, Device, DeviceUser, SyncLog
from devices.services import health, ingest, punch_sink
from devices.services.common import NS_AGENTS, bump_devices, macs_match, normalize_ip, normalize_mac, normalize_serial, now, setting

ANNOUNCE_INTERVAL_SECONDS = 3600
UPLOAD_BATCH_SIZE = 200


def _touch(model_cls, instance, /, **values) -> None:
    """Telemetry write: no version bump, no ``updated_by`` (a machine report is not an edit)."""
    model_cls.all_objects.filter(pk=instance.pk).update(**values)
    for name, value in values.items():
        if not hasattr(value, "resolve_expression"):
            setattr(instance, name, value)


def _clock_offset(device: Device, device_time, observed_at: datetime | None) -> int | None:
    """Terminal clock minus real time, in seconds (A10: measured and shown, never corrected)."""
    parsed = ingest.parse_device_time(device_time)
    if parsed is None:
        return None
    return int((ingest.localise(device, parsed) - (observed_at or now())).total_seconds())


# --------------------------------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------------------------------
def agent_devices(agent: Agent):
    return Device.objects.filter(agent=agent, is_active=True).select_related("office").order_by("name", "id")


def agent_config(agent: Agent) -> dict:
    """What the agent needs to do its job: its identity, intervals, terminals (pins included), settings."""
    return {
        "agent": agent,
        "intervals": {"heartbeat_seconds": agent.heartbeat_interval_seconds, "sync_seconds": agent.sync_interval_seconds},
        "announce_interval_seconds": int(setting("DEVICES_AGENT_ANNOUNCE_SECONDS", ANNOUNCE_INTERVAL_SECONDS)),
        "upload_batch_size": UPLOAD_BATCH_SIZE,
        "devices": list(agent_devices(agent)),
        "settings": agent.settings or {},
        "server_time": now(),
    }


def resolve_device(agent: Agent, device_uid=None, serial_number: str | None = None, *, lock: bool = False) -> Device:
    """A live device bound to ``agent``, by uid or by serial (the pin for a device not yet contacted); else 404."""
    queryset = Device.objects.select_related("office").filter(agent=agent)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    device = None
    if device_uid:
        device = queryset.filter(uid=device_uid).first()
    else:
        serial = normalize_serial(serial_number)
        if serial:
            device = queryset.filter(serial_number=serial).first() or queryset.filter(expected_serial=serial, serial_number__isnull=True).first()
    if device is None:
        raise NotFound("device_not_found", "No device is bound to this agent with that identity. Announce the device first.")
    return device


# --------------------------------------------------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def heartbeat(agent: Agent, data: dict) -> dict:
    at = now()
    values = {
        "last_heartbeat_at": at,
        "queued_records": int(data.get("queued_records") or 0),
        "failed_uploads": int(data.get("failed_uploads") or 0),
        "last_error": (data.get("last_error") or "")[:2000],
    }
    for field, limit in (("agent_version", 40), ("hostname", 120), ("platform", 120)):
        if data.get(field):
            values[field] = str(data[field])[:limit]
    if data.get("local_ip") and normalize_ip(data["local_ip"]):
        values["local_ip"] = normalize_ip(data["local_ip"])
    reached = False
    for report in data.get("devices") or []:
        try:
            device = resolve_device(agent, report.get("device"), report.get("serial_number"))
        except NotFound:
            continue
        update = {}
        if report.get("reachable"):
            reached = True
            update.update(last_seen_at=at, last_error=(report.get("last_error") or "")[:2000])
        else:
            update["last_error"] = (report.get("last_error") or f"Not reachable from {agent.code} on the office LAN.")[:2000]
        offset = _clock_offset(device, report.get("device_time"), report.get("observed_at")) if report.get("reachable") else None
        if offset is not None:
            update["clock_offset_seconds"] = offset
        _touch(Device, device, **update)
    if reached:
        values["last_device_contact_at"] = at
    _touch(Agent, agent, **values)
    bump_devices(NS_AGENTS)
    return agent_config(agent)


# --------------------------------------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------------------------------------
def _record_mismatch(agent: Agent, device: Device, *, expected_serial, reported_serial, expected_mac, reported_mac, address: str, reason: str) -> str:
    """Mark the device IDENTITY_MISMATCH (sticky) and keep the evidence; nothing is adopted from the impostor."""
    at = now()
    message = (
        f"IDENTITY_MISMATCH at {address}: {reason}. expected serial {expected_serial or 'unknown'}, reported serial {reported_serial or 'none'}; "
        f"expected MAC {expected_mac or 'unknown'}, reported MAC {reported_mac or 'none'}; agent {agent.code}; observed {at.isoformat()}"
    )
    _touch(Device, device, identity_status=Device.IdentityStatus.IDENTITY_MISMATCH, identity_message=message, identity_checked_at=at, last_error=message)
    _touch(Agent, agent, last_error=message[:2000], last_device_contact_at=at)
    SyncLog.objects.create(
        device=device,
        agent=agent,
        sync_type=SyncLog.Type.INFO,
        status=SyncLog.Status.FAILED,
        started_at=at,
        finished_at=at,
        duration_ms=0,
        error_message=message,
        details={
            "event": "IDENTITY_MISMATCH",
            "expected_serial": expected_serial,
            "reported_serial": reported_serial,
            "expected_mac": expected_mac,
            "reported_mac": reported_mac,
            "address": address,
            "agent": agent.code,
            "reason": reason,
        },
    )
    record("devices.device_identity_mismatch", obj=device, after={"expected_serial": expected_serial, "reported_serial": reported_serial, "agent": agent.code, "reason": reason})
    bump_devices()
    return message


def _record_bound_elsewhere(agent: Agent, device: Device, serial: str, address: str) -> str:
    at = now()
    owner = device.agent.code if device.agent_id else f"office {device.office.name if device.office_id else '-'}"
    message = f"{agent.code} announced {serial} at {address}, but the device is bound to {owner}. Nothing was changed; re-homing is a staff action."
    _touch(Agent, agent, last_error=message[:2000])
    SyncLog.objects.create(
        device=device,
        agent=agent,
        sync_type=SyncLog.Type.INFO,
        status=SyncLog.Status.FAILED,
        started_at=at,
        finished_at=at,
        duration_ms=0,
        error_message=message,
        details={"event": "DEVICE_BOUND_ELSEWHERE", "reported_serial": serial, "address": address, "agent": agent.code, "bound_to": owner},
    )
    record("devices.device_announce_refused", obj=device, after={"agent": agent.code, "reported_serial": serial, "bound_to": owner})
    return message


def _pin_fault(device: Device, serial: str, mac: str) -> tuple[str, str] | None:
    """``(reason, expected_mac)`` when the announced identity contradicts the device's pin, else ``None``."""
    expected_mac = device.expected_mac or device.mac_address
    if device.expected_serial and normalize_serial(device.expected_serial) != serial:
        return "the announced serial differs from the serial this device is pinned to", expected_mac
    if macs_match(expected_mac, mac) is False:
        return "the announced MAC differs from the device on file", expected_mac
    return None


def _resolve_for_announce(agent: Agent, serial: str, ip: str | None, port: int, mac: str) -> Device | None:
    queryset = Device.objects.select_for_update(of=("self",)).select_related("office", "agent")
    device = queryset.filter(serial_number=serial).first() or queryset.filter(expected_serial=serial).first()
    if device is not None or not ip:
        return device
    at_address = queryset.filter(agent=agent, ip_address=ip, port=port).first()
    if at_address is not None and at_address.serial_number:
        # same address, another serial on file: two devices, not one device moved
        _record_mismatch(
            agent,
            at_address,
            expected_serial=at_address.serial_number,
            reported_serial=serial,
            expected_mac=at_address.mac_address,
            reported_mac=mac,
            address=f"{ip}:{port}",
            reason="the announced serial differs from the device on file at this address",
        )
        return None
    return at_address


def announce(agent: Agent, data: dict) -> tuple[Device, bool]:
    """Bind or refresh a terminal the agent can read. Returns ``(device, created)``; refusals are recorded, then raised."""
    serial = normalize_serial(data.get("serial_number"))
    ip = normalize_ip(data.get("ip_address"))
    port = int(data.get("port") or 4370)
    mac = normalize_mac(data.get("mac_address"))
    address = f"{ip or 'unknown'}:{port}"
    refusal: Conflict | None = None
    with transaction.atomic():
        device = _resolve_for_announce(agent, serial, ip, port, mac)
        created = device is None
        if device is not None:
            other_agent = device.agent_id is not None and device.agent_id != agent.pk
            other_office = device.agent_id is None and device.office_id and agent.office_id and device.office_id != agent.office_id
            if other_agent or other_office:
                refusal = Conflict("device_bound_elsewhere", _record_bound_elsewhere(agent, device, serial, address))
            elif not device.is_active:
                refusal = Conflict("device_inactive", f"{device.name} is deactivated on the platform; nothing is delivered for it until it is reactivated.")
            elif fault := _pin_fault(device, serial, mac):
                message = _record_mismatch(
                    agent, device, expected_serial=device.expected_serial or device.serial_number, reported_serial=serial, expected_mac=fault[1], reported_mac=mac, address=address, reason=fault[0]
                )
                refusal = Conflict("identity_mismatch", message)
        if refusal is None:
            device = _bind(agent, device, serial=serial, ip=ip, port=port, mac=mac, data=data)
    if refusal is not None:
        raise refusal
    return device, created


def _bind(agent: Agent, device: Device | None, *, serial: str, ip, port: int, mac: str, data: dict) -> Device:
    at = now()
    hardware = {name: str(data.get(name) or "")[:120] for name in ("model", "firmware_version", "platform") if data.get(name)}
    protocol = data.get("protocol") if data.get("protocol") in Device.Protocol.values else None
    created = device is None
    if created:
        device = Device(name=(data.get("name") or serial)[:120], serial_number=serial, expected_serial=serial, expected_mac=mac, ip_address=ip, port=port, agent=agent, office=agent.office)
        if protocol:
            device.protocol = protocol
        stamp_create(device, None)
        device.save()
        record("devices.device_created", obj=device, after={"serial_number": serial, "agent": agent.code, "ip_address": ip, "source": "agent announce"})
    values = {
        "serial_number": serial,
        "port": port,
        "last_seen_at": at,
        "last_error": "",
        "identity_status": Device.IdentityStatus.VERIFIED,
        "identity_checked_at": at,
        "identity_message": f"Serial {serial} confirmed by {agent.code} at {ip or 'unknown'}:{port}" + (f", MAC {mac}" if mac else ""),
        **hardware,
    }
    if ip:
        values["ip_address"] = ip
    if mac:
        values["mac_address"] = mac
    if not device.expected_serial:
        values["expected_serial"] = serial
    if not device.expected_mac and mac:
        values["expected_mac"] = mac
    if isinstance(data.get("device_info"), dict) and data["device_info"]:
        values["device_info"] = data["device_info"]
    offset = _clock_offset(device, data.get("device_time"), data.get("observed_at"))
    if offset is not None:
        values["clock_offset_seconds"] = offset
    if device.agent_id is None:
        values["agent"] = agent
        if device.office_id is None and agent.office_id is not None:
            values["office"] = agent.office
        record("devices.device_bound", obj=device, after={"agent": agent.code, "serial_number": serial})
    _touch(Device, device, **values)
    _touch(Agent, agent, last_device_contact_at=at)
    bump_devices()
    return Device.objects.select_related("office", "agent__office").get(pk=device.pk)


@transaction.atomic
def report_identity_mismatch(agent: Agent, data: dict) -> dict:
    """The agent found a terminal answering as someone else at a known address (evidence only; no data follows)."""
    expected_serial = normalize_serial(data.get("expected_serial"))
    reported_serial = normalize_serial(data.get("reported_serial"))
    address = f"{normalize_ip(data.get('ip_address')) or 'unknown'}:{int(data.get('port') or 4370)}"
    try:
        device = resolve_device(agent, data.get("device"), expected_serial)
    except NotFound:
        message = (
            f"IDENTITY_MISMATCH at {address}: expected serial {expected_serial or 'unknown'}, reported serial {reported_serial or 'none'}; no device is bound to {agent.code} under the expected serial"
        )
        _touch(Agent, agent, last_error=message[:2000], last_device_contact_at=now())
        return {"recorded": True, "device": None, "identity_status": Device.IdentityStatus.IDENTITY_MISMATCH, "message": message}
    message = _record_mismatch(
        agent,
        device,
        expected_serial=expected_serial or device.serial_number,
        reported_serial=reported_serial,
        expected_mac=normalize_mac(data.get("expected_mac")) or device.expected_mac or device.mac_address,
        reported_mac=normalize_mac(data.get("reported_mac")),
        address=address,
        reason=(data.get("reason") or "the agent's identity check failed")[:500],
    )
    return {"recorded": True, "device": device.uid, "identity_status": Device.IdentityStatus.IDENTITY_MISMATCH, "message": message}


@transaction.atomic
def record_discovery(agent: Agent, data: dict) -> dict:
    """A LAN scan can locate this agent's terminals by the serial they state; it never decides who a terminal is."""
    at = now()
    found = list(data.get("found") or [])
    devices = list(agent_devices(agent).select_for_update(of=("self",)))
    by_pin = {}
    for device in devices:
        pin = normalize_serial(device.expected_serial or device.serial_number)
        if pin:
            by_pin[pin] = device
    matches, unmatched, matched = [], [], set()
    for host in found:
        serial = normalize_serial(host.get("serial_number"))
        device = by_pin.get(serial) if serial else None
        if device is None:
            unmatched.append(host)
            continue
        ip, port = normalize_ip(host.get("ip_address")), int(host.get("port") or 4370)
        mac = normalize_mac(host.get("mac_address"))
        expected_mac = device.expected_mac or device.mac_address
        if macs_match(expected_mac, mac) is False:
            message = _record_mismatch(
                agent,
                device,
                expected_serial=device.expected_serial or device.serial_number,
                reported_serial=serial,
                expected_mac=expected_mac,
                reported_mac=mac,
                address=f"{ip}:{port}",
                reason="a terminal answering with this serial reported a different MAC",
            )
            matches.append({"device": device.uid, "reported_serial": serial, "ip_address": ip, "mac_address": mac, "matched": False, "identity_status": device.identity_status, "message": message})
            continue
        values = {
            "ip_address": ip or device.ip_address,
            "port": port,
            "serial_number": serial,
            "identity_status": Device.IdentityStatus.VERIFIED,
            "identity_checked_at": at,
            "identity_message": f"Serial {serial} confirmed by {agent.code} during an on-site scan at {ip}:{port}",
            "last_seen_at": at,
            "last_error": "",
        }
        if mac:
            values["mac_address"] = mac
            if not device.expected_mac:
                values["expected_mac"] = mac
        for field in ("firmware_version", "platform"):
            if host.get(field):
                values[field] = str(host[field])[:120]
        if host.get("device_name"):
            values["model"] = str(host["device_name"])[:120]
        _touch(Device, device, **values)
        matched.add(device.pk)
        matches.append(
            {
                "device": device.uid,
                "reported_serial": serial,
                "ip_address": device.ip_address,
                "mac_address": mac,
                "matched": True,
                "identity_status": device.identity_status,
                "message": values["identity_message"],
            }
        )
    SyncLog.objects.create(
        device=next((device for device in devices if device.pk in matched), None),
        agent=agent,
        sync_type=SyncLog.Type.INFO,
        status=SyncLog.Status.SUCCESS if matched else SyncLog.Status.PARTIAL,
        started_at=data.get("scanned_at") or at,
        finished_at=at,
        records_read=len(found),
        details={
            "event": "LAN_DISCOVERY",
            "subnet": data.get("subnet"),
            "agent_ip": data.get("agent_ip"),
            "gateway": data.get("gateway"),
            "hosts_scanned": data.get("hosts_scanned"),
            "hosts_open": data.get("hosts_open"),
            "matched": sorted(match["reported_serial"] for match in matches if match["matched"]),
            "unmatched_hosts": [host.get("ip_address") for host in unmatched],
        },
    )
    if matched:
        _touch(Agent, agent, last_device_contact_at=at)
        bump_devices()
    still = [device.expected_serial or device.name for device in devices if device.pk not in matched and health.awaiting_discovery(device)]
    return {"recorded": True, "matches": matches, "unmatched": unmatched, "still_awaiting": still}


# --------------------------------------------------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------------------------------------------------
USER_FIELDS = ("device_uid", "name", "privilege", "card", "group_id", "has_password", "raw_payload")


def _user_values(entry: dict) -> dict:
    return {
        "device_uid": entry.get("device_uid"),
        "name": str(entry.get("name") or "").strip()[:150],
        "privilege": entry.get("privilege"),
        "card": str(entry.get("card") or "").strip()[:40],
        "group_id": str(entry.get("group_id") or "").strip()[:40],
        "has_password": bool(entry.get("has_password")),
        "raw_payload": entry.get("raw_payload") if isinstance(entry.get("raw_payload"), dict) else {},
    }


def upsert_users(device: Device, users, *, at: datetime) -> tuple[int, int, int, list[str]]:
    """Upsert terminal users (never deletes). Returns ``(created, updated, invalid, pins_present)``."""
    existing = {row.pin: row for row in DeviceUser.objects.select_for_update(of=("self",)).filter(device=device)}
    created, changed, present, invalid = [], [], [], 0
    for entry in users:
        pin = str(entry.get("pin") or "").strip()
        if not pin or len(pin) > 80:
            invalid += 1
            continue
        if pin in present:
            continue
        present.append(pin)
        values = _user_values(entry)
        row = existing.get(pin)
        if row is None:
            row = DeviceUser(device=device, pin=pin, first_seen_at=at, last_seen_at=at, **values)
            stamp_create(row, None)
            created.append(row)
        else:
            for name, value in values.items():
                setattr(row, name, value)
            row.last_seen_at, row.updated_at = at, at
            changed.append(row)
    if created:
        DeviceUser.objects.bulk_create(created)
    if changed:
        DeviceUser.objects.bulk_update(changed, [*USER_FIELDS, "last_seen_at", "updated_at"])
    return len(created), len(changed), invalid, present


@transaction.atomic
def sync_users(agent: Agent, device: Device, users: list, *, read_at: datetime | None = None) -> dict:
    """A whole-table read of one terminal: upsert, then the USERS log that is the presence watermark."""
    started = now()
    created, updated, invalid, present = upsert_users(device, users, at=started)
    finished = now()
    _touch(Device, device, user_count=len(present), last_sync_at=finished)
    _touch(Agent, agent, last_sync_at=finished)
    SyncLog.objects.create(
        device=device,
        agent=agent,
        sync_type=SyncLog.Type.USERS,
        status=SyncLog.Status.PARTIAL if invalid else SyncLog.Status.SUCCESS,
        started_at=started,
        finished_at=finished,
        duration_ms=int((finished - started).total_seconds() * 1000),
        records_read=len(users),
        records_new=created,
        records_duplicate=updated,
        error_message=f"{invalid} user entr{'y' if invalid == 1 else 'ies'} without a usable PIN; this read is not used to decide who left the terminal." if invalid else "",
        details={
            "agent": agent.code,
            "transport": ingest.AGENT_PUSH,
            "users_seen_at": started.isoformat(),
            "users_present": sorted(present),
            "invalid": invalid,
            "read_at": read_at.isoformat() if read_at else None,
        },
    )
    bump_devices(NS_AGENTS)
    return {"received": len(users), "created": created, "updated": updated, "invalid": invalid}


@transaction.atomic
def sync_attendance(agent: Agent, device: Device, records: list, *, batch_id: str = "") -> dict:
    result = ingest.ingest(device, records, source=ingest.AGENT_PUSH, agent=agent, batch_id=batch_id)
    _touch(Agent, agent, last_sync_at=now())
    return result


def sync_status(device: Device) -> dict:
    """What the server already holds, so an agent can resume rather than resend (a hint: dedup decides)."""
    status = punch_sink.get().status(device)
    return {
        "device": device.uid,
        "stored_records": status.get("stored_records") if status.get("stored_records") is not None else device.attendance_count,
        "highest_device_record_uid": status.get("highest_device_record_uid"),
        "latest_device_time": status["latest_device_time"].isoformat() if isinstance(status.get("latest_device_time"), datetime) else status.get("latest_device_time"),
        "last_sync_at": device.last_sync_at,
        "punch_store_installed": punch_sink.installed(),
        "server_time": now(),
    }
