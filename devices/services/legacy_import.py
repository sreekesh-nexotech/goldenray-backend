"""eSSL import (PLAN §7.5 "devices/agents (new service credentials issued; agents reconfigured)", "device_users links
kept as per-device links plus a report of PINs linked on more than one device").

Every function takes plain row dicts exactly as the eSSL tables hold them (``SELECT *``) and returns
``{"created", "updated", "skipped", "violations"}``: each source row is tracked in ``core_legacy_map`` (``ESSL`` ×
table × id), a re-run updates only changed columns and never duplicates, a platform row deleted after the import stays
deleted (``skipped``), rows that cannot be imported are skipped and listed, rows imported with a repair are listed too.
Each call writes one ``devices.legacy_imported`` audit row (counts + sha256 of the batch). Offices and employees resolve
through the hr import's map, so :func:`hr.services.legacy_import.import_all` runs first.

Order (:func:`import_all`): agents → devices → device users → presence watermarks → protocol mappings → the ADMS
quarantine list → the ADMS evidence of the last 30 days.

* **Agents** get a new platform credential (the eSSL bcrypt token hashes are not portable and are dropped); the
  plaintext tokens are returned once under ``credentials`` for reconfiguring the office agents (C6). An agent revoked
  in eSSL is imported disabled without a credential (reported).
* **Devices**: ``is_online`` is not migrated (health is derived); ``protocol`` ADMS_PUSH (a no-op value) becomes ZK_TCP;
  ``adms_enabled`` is imported **off** — the terminal must be re-pointed to ``/iclock/<device_token>/`` with a token
  issued by ``adms/enable/`` (reported); MACs are normalised; invalid addresses/ports/timeouts are repaired (reported);
  ``serial_verified_at`` fills ``identity_checked_at``; ``last_punch_at`` (naive device time) is read in the office zone.
* **Device users** keep their per-device links; PINs linked on more than one device are reported for HR to confirm
  (eSSL's ``map-pin`` linked a PIN on every terminal, spec §I.8).
* **Watermarks**: the latest USERS sync log of each device (and its latest successful one) so presence states survive.
* **ADMS evidence**: ``adms_requests`` younger than the retention window (30 days) with the receiver's own redaction
  (eSSL kept terminal-user passwords in the stored bodies); older requests are not migrated (PLAN §7.5). The
  quarantine list keeps its counters; every eSSL entry had the one reason eSSL knew (``UNKNOWN_SERIAL``).
"""

from __future__ import annotations

import zoneinfo
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from core import service_credentials
from core.models import LegacyMap, ServiceCredential
from devices.models import AdmsRequest, AdmsUnknownDevice, Agent, Device, DeviceUser, ProtocolMapping, SyncLog
from devices.models.adms import BODY_EXCERPT_CHARS, MAX_STORED_BODY_BYTES
from devices.services import adms, adms_evidence
from devices.services.common import NS_AGENTS, NS_DEVICE_USERS, NS_DEVICES, NS_PROTOCOL, normalize_ip, normalize_mac, normalize_serial
from flarize.cache_utils import bump
from flarize.logging import redact_path
from hr.models import Employee, Office
from hr.services.legacy_import import ESSL, Report, Skip, _dt, _text, _upsert, checksum, mapped, mapped_id  # the shared eSSL import helpers (same HR context)

AGENT_LIMITS = {"heartbeat_interval_seconds": (10, 3600, 60), "sync_interval_seconds": (30, 86400, 300), "degraded_queue_threshold": (1, 10_000_000, 500)}
IDENTITY_STATUSES = set(Device.IdentityStatus.values)


def _run(rows: Iterable[dict], handle: Callable[[dict, Report], None], *, table: str, object_type: str, user, namespaces: tuple[str, ...], after: Callable[[Report], None] | None = None) -> Report:
    rows = list(rows)
    report = Report()
    for row in rows:
        try:
            with transaction.atomic():
                handle(row, report)
        except Skip:
            continue
        except IntegrityError as exc:
            report.violation(row.get("id"), "row", f"rejected by the database: {str(exc).splitlines()[0]}")
    if after is not None:
        after(report)
    record(
        "devices.legacy_imported",
        object_type=object_type,
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={
            "source": f"ESSL {table}",
            "rows": len(rows),
            "checksum": checksum(rows),
            "created": report.created,
            "updated": report.updated,
            "skipped": report.skipped,
            "violations": len(report.violations),
        },
    )
    for namespace in namespaces:
        bump(namespace)
    return report


def _office(row: dict, report: Report):
    if row.get("office_id") is None:
        return None
    office = mapped(Office, "offices", row["office_id"])
    if office is None:
        report.violation(row.get("id"), "office_id", f"office {row['office_id']} was not imported; left empty")
    return office


def _int_in(row: dict, field: str, low: int, high: int, default: int, report: Report) -> int:
    value = row.get(field)
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    if value is not None:
        report.violation(row.get("id"), field, f"{value!r} is outside {low}–{high}; set to {default}")
    return default


# --------------------------------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------------------------------
def import_agents(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``agents`` → ``devices_agent`` + a new service credential per active agent (tokens returned once)."""
    credentials: list[dict] = []

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        code, name = _text(row.get("code")), _text(row.get("name"))
        if not code or not name:
            report.violation(source_id, "row", "code and name are required")
            raise Skip
        revoked = row.get("token_revoked_at") is not None or not row.get("is_active", True)
        values = {name_: _int_in(row, name_, low, high, default, report) for name_, (low, high, default) in AGENT_LIMITS.items()}
        offline = row.get("offline_after_seconds")
        values["offline_after_seconds"] = offline if isinstance(offline, int) and offline >= values["heartbeat_interval_seconds"] else max(300, values["heartbeat_interval_seconds"])
        if values["offline_after_seconds"] != offline:
            report.violation(source_id, "offline_after_seconds", f"{offline!r} is shorter than the heartbeat interval; set to {values['offline_after_seconds']}")
        local_ip = normalize_ip(row.get("local_ip"))
        if row.get("local_ip") and local_ip is None:
            report.violation(source_id, "local_ip", f"{row['local_ip']!r} is not an IP address; left empty")
        values.update(
            code=code[:60],
            name=name[:120],
            office=_office(row, report),
            agent_version=_text(row.get("version"))[:40],
            hostname=_text(row.get("hostname"))[:120],
            platform=_text(row.get("platform"))[:120],
            local_ip=local_ip,
            last_heartbeat_at=_dt(row.get("last_heartbeat_at")),
            last_device_contact_at=_dt(row.get("last_device_contact_at")),
            last_sync_at=_dt(row.get("last_sync_at")),
            last_error=_text(row.get("last_error")),
            queued_records=max(0, int(row.get("queued_records") or 0)),
            failed_uploads=max(0, int(row.get("failed_uploads") or 0)),
            settings=row.get("settings") if isinstance(row.get("settings"), dict) else {},
            notes=_text(row.get("notes")),
            is_active=not revoked,
        )
        created_before = mapped_id("agents", source_id) is None
        agent = _upsert(Agent, table="agents", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))
        if agent is None or not created_before:
            return
        if revoked:
            report.violation(source_id, "token_revoked_at", "revoked in eSSL: imported disabled, no credential issued (rotate the token to bring it back)")
            return
        credential, token = service_credentials.issue(ServiceCredential.Kind.AGENT, f"Office agent {agent.code}"[:120], agent, user=user)
        Agent.all_objects.filter(pk=agent.pk).update(credential=credential)
        credentials.append({"agent_code": agent.code, "token": token})

    report = _run(rows, handle, table="agents", object_type="devices.agent", user=user, namespaces=(NS_AGENTS,))
    return {**report.as_dict(), "credentials": credentials}


# --------------------------------------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------------------------------------
def _zone(office) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(office.timezone if office is not None else settings.TIME_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def import_devices(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``devices`` → ``devices_device`` (see the module docstring for the repairs)."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        name = _text(row.get("name"))
        if not name:
            report.violation(source_id, "name", "a device needs a name")
            raise Skip
        office = _office(row, report)
        agent = None
        if row.get("agent_id") is not None:
            agent = mapped(Agent, "agents", row["agent_id"])
            if agent is None:
                report.violation(source_id, "agent_id", f"agent {row['agent_id']} was not imported; unassigned")
        ip = normalize_ip(row.get("ip_address"))
        if row.get("ip_address") and ip is None:
            report.violation(source_id, "ip_address", f"{row['ip_address']!r} is not an IP address; left empty (the agent can discover it)")
        serial, expected = normalize_serial(row.get("serial_number")), normalize_serial(row.get("expected_serial"))
        if ip is None and serial is None and expected is None:
            report.violation(source_id, "row", "no address and no serial: nothing could ever locate or verify this device")
            raise Skip
        macs = {}
        for field in ("mac_address", "expected_mac"):
            macs[field] = normalize_mac(row.get(field))
            if row.get(field) and not macs[field]:
                report.violation(source_id, field, f"{row[field]!r} is not a MAC address; left empty")
        protocol = _text(row.get("protocol")).upper() or Device.Protocol.ZK_TCP
        if protocol not in Device.Protocol.values:
            report.violation(source_id, "protocol", f"{protocol} has no meaning on the platform (the transport is derived); ZK_TCP")
            protocol = Device.Protocol.ZK_TCP
        if row.get("adms_enabled"):
            report.violation(source_id, "adms_enabled", "ADMS push imported OFF: enable it in Studio to issue the /iclock/<device_token>/ token and re-point the terminal")
        identity = _text(row.get("identity_status")).upper() or Device.IdentityStatus.UNVERIFIED
        if identity not in IDENTITY_STATUSES:
            report.violation(source_id, "identity_status", f"unknown value {identity!r}; UNVERIFIED")
            identity = Device.IdentityStatus.UNVERIFIED
        last_punch = _dt(row.get("last_punch_at"))
        if last_punch is not None and last_punch.tzinfo is None:
            last_punch = last_punch.replace(tzinfo=_zone(office))
        adms_port = row.get("adms_port")
        values = {
            "name": name[:120],
            "serial_number": serial,
            "expected_serial": expected,
            "expected_mac": macs["expected_mac"],
            "mac_address": macs["mac_address"],
            "office": office,
            "agent": agent,
            "ip_address": ip,
            "port": _int_in(row, "port", 1, 65535, 4370, report),
            "protocol": protocol,
            "comm_password": _int_in(row, "comm_password", 0, 999999, 0, report),
            "timeout_seconds": _int_in(row, "timeout_seconds", 1, 120, 15, report),
            "model": _text(row.get("model"))[:120],
            "firmware_version": _text(row.get("firmware_version"))[:120],
            "platform": _text(row.get("platform"))[:120],
            "adms_enabled": False,
            "adms_registration_state": row.get("adms_registration_state") if row.get("adms_registration_state") in Device.RegistrationState.values else Device.RegistrationState.NEVER_SEEN,
            "adms_last_seen_at": _dt(row.get("adms_last_seen_at")),
            "adms_last_handshake_at": _dt(row.get("adms_last_handshake_at")),
            "adms_last_push_at": _dt(row.get("adms_last_push_at")),
            "adms_last_command_poll_at": _dt(row.get("adms_last_command_poll_at")),
            "adms_source_ip": normalize_ip(row.get("adms_source_ip")),
            "adms_request_count": max(0, int(row.get("adms_request_count") or 0)),
            "adms_options": row.get("adms_options") if isinstance(row.get("adms_options"), dict) else {},
            "adms_server": _text(row.get("adms_server"))[:120],
            "adms_port": adms_port if isinstance(adms_port, int) and 1 <= adms_port <= 65535 else None,
            "identity_status": identity,
            "identity_message": _text(row.get("identity_message")),
            "identity_checked_at": _dt(row.get("identity_checked_at")) or _dt(row.get("serial_verified_at")),
            "last_seen_at": _dt(row.get("last_seen_at")),
            "last_sync_at": _dt(row.get("last_sync_at")),
            "last_punch_at": last_punch,
            "last_error": _text(row.get("last_error")),
            "user_count": max(0, int(row.get("user_count") or 0)),
            "attendance_count": max(0, int(row.get("attendance_count") or 0)),
            "device_info": row.get("device_info") if isinstance(row.get("device_info"), dict) else {},
            "notes": _text(row.get("notes")),
            "is_active": bool(row.get("is_active", True)),
        }
        _upsert(Device, table="devices", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="devices", object_type="devices.device", user=user, namespaces=(NS_DEVICES,)).as_dict()


# --------------------------------------------------------------------------------------------------------------------
# Device users
# --------------------------------------------------------------------------------------------------------------------
def _multi_device_pins(report: Report) -> None:
    """PINs linked on more than one device: kept per device (A1), listed for HR to confirm each link."""
    links = defaultdict(list)
    imported = LegacyMap.objects.filter(source_system=ESSL, source_table="device_users").values_list("target_id", flat=True)
    for row in DeviceUser.objects.filter(pk__in=list(imported), employee__isnull=False).select_related("device", "employee").order_by("pin", "device__name"):
        links[row.pin].append(f"{row.device.name} → {row.employee.code}")
    for pin, entries in sorted(links.items()):
        if len(entries) > 1:
            report.violation(pin, "pin", f"PIN {pin} is linked on {len(entries)} devices ({'; '.join(entries)}): identity is per device now — confirm each link")


def import_device_users(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``device_users`` → ``devices_device_user`` (per-device links kept; multi-device PINs reported)."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        device = mapped(Device, "devices", row.get("device_id")) if row.get("device_id") is not None else None
        pin = _text(row.get("device_user_id"))
        if device is None:
            report.violation(source_id, "device_id", f"device {row.get('device_id')} was not imported; row skipped")
            raise Skip
        if not pin or len(pin) > 80:
            report.violation(source_id, "device_user_id", "no usable PIN; row skipped")
            raise Skip
        employee = None
        if row.get("employee_id") is not None:
            employee = mapped(Employee, "employees", row["employee_id"])
            if employee is None:
                report.violation(source_id, "employee_id", f"employee {row['employee_id']} was not imported; left unlinked")
        privilege = row.get("privilege")
        values = {
            "device": device,
            "pin": pin,
            "device_uid": row.get("device_uid"),
            "name": _text(row.get("name"))[:150],
            "privilege": privilege if isinstance(privilege, int) and -32768 <= privilege <= 32767 else None,
            "card": _text(row.get("card"))[:40],
            "group_id": _text(row.get("group_id"))[:40],
            "has_password": bool(row.get("has_password")),
            "employee": employee,
            "raw_payload": row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {},
            "first_seen_at": _dt(row.get("first_seen_at")),
            "last_seen_at": _dt(row.get("last_seen_at")),
        }
        _upsert(DeviceUser, table="device_users", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="device_users", object_type="devices.deviceuser", user=user, namespaces=(NS_DEVICE_USERS,), after=_multi_device_pins).as_dict()


# --------------------------------------------------------------------------------------------------------------------
# Presence watermarks
# --------------------------------------------------------------------------------------------------------------------
def _latest_users_logs(rows: list[dict]) -> list[dict]:
    """Per device: the latest USERS log and the latest SUCCESS USERS log (often the same row)."""
    keep = {}
    ordered = sorted((row for row in rows if row.get("sync_type") == "USERS" and row.get("device_id") is not None), key=lambda row: (str(row.get("started_at")), row.get("id") or 0))
    for row in ordered:
        keep[(row["device_id"], "latest")] = row
        if row.get("status") == "SUCCESS":
            keep[(row["device_id"], "success")] = row
    return sorted({row["id"]: row for row in keep.values()}.values(), key=lambda row: row["id"])


def import_user_watermarks(rows: Iterable[dict], *, user=None) -> dict:
    """The presence watermark of every device (its latest USERS logs); older logs are not migrated."""
    rows = list(rows)
    selected = _latest_users_logs(rows)

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        if mapped_id("sync_logs", source_id) is not None:
            report.skipped += 1  # logs are immutable: nothing to update
            return
        device = mapped(Device, "devices", row["device_id"])
        if device is None:
            report.violation(source_id, "device_id", f"device {row['device_id']} was not imported; log skipped")
            raise Skip
        status = row.get("status") if row.get("status") in SyncLog.Status.values else SyncLog.Status.FAILED
        log = SyncLog.objects.create(
            device=device,
            agent=mapped(Agent, "agents", row.get("agent_id")) if row.get("agent_id") is not None else None,
            sync_type=SyncLog.Type.USERS,
            status=status,
            started_at=_dt(row.get("started_at")),
            finished_at=_dt(row.get("finished_at")),
            duration_ms=max(0, int(row.get("duration_ms") or 0)),
            records_read=max(0, int(row.get("records_read") or 0)),
            records_new=max(0, int(row.get("records_new") or 0)),
            records_duplicate=max(0, int(row.get("records_duplicate") or 0)),
            error_message=_text(row.get("error_message")),
            details={**(row.get("details") if isinstance(row.get("details"), dict) else {}), "imported_from": "eSSL sync_logs"},
        )
        LegacyMap.objects.create(source_system=ESSL, source_table="sync_logs", source_id=str(source_id), target_table=SyncLog._meta.db_table, target_id=log.pk)
        report.created += 1

    report = _run(selected, handle, table="sync_logs", object_type="devices.synclog", user=user, namespaces=(NS_DEVICE_USERS,))
    result = report.as_dict()
    result["skipped"] += len(rows) - len(selected)  # older and non-USERS logs are not migrated by design
    return result


# --------------------------------------------------------------------------------------------------------------------
# Protocol mappings
# --------------------------------------------------------------------------------------------------------------------
def import_protocol_mappings(rows: Iterable[dict], *, user=None) -> dict:
    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        field, meaning_type = _text(row.get("field")).lower(), _text(row.get("meaning_type")).upper()
        if field not in ProtocolMapping.Field.values or meaning_type not in ProtocolMapping.MeaningType.values or not _text(row.get("meaning_code")) or not isinstance(row.get("raw_value"), int):
            report.violation(source_id, "row", "field, raw_value, meaning_type and meaning_code must be valid")
            raise Skip
        confidence = _text(row.get("confidence")).upper() or ProtocolMapping.Confidence.UNKNOWN
        if confidence not in ProtocolMapping.Confidence.values:
            report.violation(source_id, "confidence", f"unknown value {confidence!r}; UNKNOWN")
            confidence = ProtocolMapping.Confidence.UNKNOWN
        values = {
            "device_platform": _text(row.get("device_platform"))[:120],
            "firmware_version": _text(row.get("firmware_version"))[:120],
            "field": field,
            "raw_value": row["raw_value"],
            "meaning_type": meaning_type,
            "meaning_code": _text(row.get("meaning_code")).upper()[:40],
            "label": _text(row.get("label"))[:120],
            "confidence": confidence,
            "notes": _text(row.get("notes")),
        }
        _upsert(ProtocolMapping, table="protocol_mappings", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="protocol_mappings", object_type="devices.protocolmapping", user=user, namespaces=(NS_PROTOCOL,)).as_dict()


# --------------------------------------------------------------------------------------------------------------------
# ADMS evidence and quarantine
# --------------------------------------------------------------------------------------------------------------------
def import_adms_unknown_devices(rows: Iterable[dict], *, user=None) -> dict:
    """eSSL ``adms_unknown_devices`` → the quarantine list (eSSL had one reason: no registered device had the serial)."""

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        serial = normalize_serial(row.get("serial_number"))
        if not serial:
            report.violation(source_id, "serial_number", "no serial; row skipped")
            raise Skip
        values = {
            "serial_number": serial[:80],
            "first_seen_at": _dt(row.get("first_seen_at")) or _dt(row.get("created_at")),
            "last_seen_at": _dt(row.get("last_seen_at")) or _dt(row.get("created_at")),
            "request_count": max(0, int(row.get("request_count") or 0)),
            "last_source_ip": normalize_ip(row.get("last_source_ip")),
            "last_path": _text(row.get("last_path"))[:255],
            "last_body_excerpt": adms.scrub(_text(row.get("last_body_excerpt")))[:BODY_EXCERPT_CHARS],
            "last_reason": AdmsUnknownDevice.Reason.UNKNOWN_SERIAL,
            "notes": _text(row.get("notes")),
        }
        _upsert(AdmsUnknownDevice, table="adms_unknown_devices", source_id=source_id, values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at")))

    return _run(rows, handle, table="adms_unknown_devices", object_type="devices.admsunknowndevice", user=user, namespaces=(NS_DEVICES,)).as_dict()


def _redacted(kind: str, body, text: str) -> tuple[bytes | None, str, bool]:
    """The receiver's own rules applied to eSSL evidence: no biometric bodies, terminal-user passwords/templates masked."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif isinstance(body, memoryview):
        body = bytes(body)
    if kind in adms.BIOMETRIC_KINDS:
        return None, "", body is not None
    text, changed = adms.redact_secrets(text)
    if body:
        decoded, encoding = adms.decode_best_effort(body)
        decoded, body_changed = adms.redact_secrets(decoded)
        if body_changed:
            body = decoded.encode(encoding if encoding in adms.DECODE_CANDIDATES else "utf-8", errors="replace")
        changed = changed or body_changed
    return body, text, changed


def import_adms_requests(rows: Iterable[dict], *, user=None, now=None) -> dict:
    """eSSL ``adms_requests`` of the retention window (30 days) → ``devices_adms_request``; older evidence is not
    migrated (PLAN §7.5), secrets are redacted as the receiver does, rows are immutable (a re-run skips them)."""
    rows = list(rows)
    cutoff = (now or timezone.now()) - timedelta(days=adms_evidence.retention_days())
    recent = [row for row in rows if (_dt(row.get("received_at")) is not None and _dt(row.get("received_at")) >= cutoff)]

    def handle(row: dict, report: Report) -> None:
        source_id = row.get("id")
        if mapped_id("adms_requests", source_id) is not None:
            report.skipped += 1
            return
        kind = row.get("request_kind") if row.get("request_kind") in AdmsRequest.Kind.values else AdmsRequest.Kind.UNKNOWN
        body, text, redacted = _redacted(kind, row.get("body"), _text(row.get("body_text")))
        extra = dict(row.get("extra") if isinstance(row.get("extra"), dict) else {})
        if isinstance(extra.get("parsed"), list):
            extra["parsed"] = [{key: ("***" if str(key).lower() in adms.SECRET_KEYS else value) for key, value in item.items()} if isinstance(item, dict) else item for item in extra["parsed"]]
        extra["imported_from"] = "eSSL adms_requests"
        if redacted:
            extra["redacted"] = "terminal-user passwords and biometric templates"
        device = mapped(Device, "devices", row["device_id"]) if row.get("device_id") is not None else None
        request_id = adms._next_id()
        AdmsRequest.objects.create(
            id=request_id,
            received_at=_dt(row["received_at"]),
            client_ip=normalize_ip(row.get("client_ip")),
            peer_ip=normalize_ip(row.get("peer_ip")),
            method=_text(row.get("method")).upper()[:10] or "GET",
            path=redact_path(_text(row.get("path")))[:255],
            raw_query=adms.scrub(_text(row.get("raw_query")))[:4000],
            query=adms.scrub_deep(row.get("query") if isinstance(row.get("query"), dict) else {}),
            headers=adms.safe_headers(row.get("headers") if isinstance(row.get("headers"), dict) else {}),
            content_type=_text(row.get("content_type"))[:160],
            device_serial=_text(row.get("device_serial"))[:80],
            device=device,
            request_kind=kind,
            table_name=_text(row.get("table_name"))[:40],
            body=body[:MAX_STORED_BODY_BYTES] if body else None,
            body_bytes=max(0, int(row.get("body_bytes") or 0)),
            body_text=adms.scrub(text)[:BODY_EXCERPT_CHARS],
            body_encoding=_text(row.get("body_encoding"))[:30],
            body_truncated=bool(row.get("body_truncated")) or bool(body and len(body) > MAX_STORED_BODY_BYTES),
            response_status=int(row.get("response_status") or 200),
            response_body=_text(row.get("response_body"))[:4000],
            records_parsed=max(0, int(row.get("records_parsed") or 0)),
            records_new=max(0, int(row.get("records_new") or 0)),
            records_duplicate=max(0, int(row.get("records_duplicate") or 0)),
            records_invalid=max(0, int(row.get("records_invalid") or 0)),
            parse_error=_text(row.get("parse_error")),
            extra=adms.scrub_deep(extra),
        )
        LegacyMap.objects.create(source_system=ESSL, source_table="adms_requests", source_id=str(source_id), target_table=AdmsRequest._meta.db_table, target_id=request_id)
        report.created += 1

    report = _run(recent, handle, table="adms_requests", object_type="devices.admsrequest", user=user, namespaces=())
    result = report.as_dict()
    result["skipped"] += len(rows) - len(recent)  # outside the 30-day evidence window: not migrated by design
    return result


def import_all(tables: dict[str, list[dict]], *, user=None, now=None) -> dict[str, dict]:
    """Every eSSL device table in dependency order (after the hr import; ``tables`` keyed by the eSSL table name)."""
    return {
        "agents": import_agents(tables.get("agents", []), user=user),
        "devices": import_devices(tables.get("devices", []), user=user),
        "device_users": import_device_users(tables.get("device_users", []), user=user),
        "sync_logs": import_user_watermarks(tables.get("sync_logs", []), user=user),
        "protocol_mappings": import_protocol_mappings(tables.get("protocol_mappings", []), user=user),
        "adms_unknown_devices": import_adms_unknown_devices(tables.get("adms_unknown_devices", []), user=user),
        "adms_requests": import_adms_requests(tables.get("adms_requests", []), user=user, now=now),
    }
