"""The ADMS push receiver behind ``/iclock/<device_token>/…`` (flag ``ADMS_RECEIVER``; eSSL ``adms_service``).

Pipeline for every request (the view only reads the HTTP request and writes the text reply):

1. **Evidence.** One ``devices_adms_request`` row per request, whatever happens next: method, the path with the token
   redacted, query, headers (credentials dropped), the body (stored copy ≤ 1 MiB, ``body_bytes`` = real size;
   terminal-user passwords and biometric templates redacted; ATTPHOTO / BIODATA bodies never stored), what it resolved
   to, what was parsed and what was answered.
2. **Identity: serial AND token.** The path token resolves a device by its sha256; the ``SN`` the request states must be
   that device's serial (or its pin while it has never reported one). No serial, an unknown token or a serial that does
   not belong to the token → nothing is ingested; a stated serial goes to the quarantine list
   (``UNKNOWN_SERIAL`` / ``TOKEN_MISMATCH``). The source address never identifies a device.
3. **Admission.** The device must be active, ``adms_enabled``, and — when it has an allow-list — push from an allowed
   address (the trusted client IP). A refused request is evidence only and does not count as contact.
4. **Interpretation.** HANDSHAKE → the option block (``TransFlag=AttLog OpLog`` only: no photos, templates or user
   pictures; no ``TimeZone``, so the terminal's clock is never set); ATTLOG → punches through the shared ingestion
   (content dedup across transports, ``attendance.punches_ingested``; the recompute runs in Celery, never here);
   USERINFO / OPERLOG ``USER`` lines → device users (per device; no presence watermark — a push is not a whole-table
   read); GETREQUEST → "OK" (the receiver never issues a command); anything else → recorded.

Every reply is HTTP 200 ``text/plain``. Refused or quarantined pushes are acknowledged ("OK", so an unknown terminal
does not retry forever); a push whose ingestion failed inside this server is answered ``ERROR`` so the terminal
resends it (eSSL acknowledged it and kept the punches only as evidence, spec §I.23).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.db.models import F

from devices.models import AdmsRequest, AdmsUnknownDevice, Device
from devices.models.adms import BODY_EXCERPT_CHARS, MAX_STORED_BODY_BYTES
from devices.services import agent_protocol, ingest
from devices.services.common import NS_DEVICE_USERS, bump_devices, ip_allowed, normalize_serial, now, setting
from devices.services.devices import device_for_token

logger = logging.getLogger("flarize.devices.adms")

DECODE_CANDIDATES = ("utf-8", "gb18030", "latin-1")
SERIAL_KEYS = ("SN", "sn", "DeviceSN", "deviceSN", "serialNumber")
TABLE_KINDS = ("ATTLOG", "OPERLOG", "USERINFO", "ATTPHOTO", "BIODATA")
BIOMETRIC_KINDS = ("ATTPHOTO", "BIODATA")
SECRET_KEYS = {"passwd", "password", "pwd", "tmp", "template", "content", "face", "photo"}
DROPPED_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
OK = "OK"
ERROR = "ERROR"


@dataclass
class Incoming:
    token: str
    endpoint: str
    method: str
    path: str
    raw_query: str
    query: dict[str, Any]
    headers: dict[str, str]
    body: bytes
    body_bytes: int
    truncated: bool
    client_ip: str | None
    peer_ip: str | None
    content_type: str = ""


@dataclass
class Outcome:
    reply: str = OK
    device: Device | None = None
    parse_error: str = ""
    records: dict = field(default_factory=lambda: {"parsed": 0, "new": 0, "duplicate": 0, "invalid": 0})
    extra: dict = field(default_factory=dict)
    store_body: bool = True
    body_override: bytes | None = None


# --------------------------------------------------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------------------------------------------------
def scrub(value: str) -> str:
    """Postgres text/jsonb cannot hold NUL; the exact bytes stay in ``body``."""
    return value.replace("\x00", "") if isinstance(value, str) else value


def scrub_deep(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_deep(item) for item in value]
    if isinstance(value, dict):
        return {scrub(str(key)): scrub_deep(item) for key, item in value.items()}
    return value


def decode_best_effort(raw: bytes) -> tuple[str, str]:
    for encoding in DECODE_CANDIDATES:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1/replace"  # pragma: no cover - latin-1 decodes any byte


def flatten_query(query: dict) -> dict:
    return {str(key): ([str(item) for item in value] if len(value) > 1 else str(value[0])) if isinstance(value, list) else str(value) for key, value in query.items()}


def extract_serial(query: dict) -> str | None:
    for key in SERIAL_KEYS:
        value = query.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        serial = normalize_serial(value)
        if serial:
            return serial[:80]
    return None


def classify(method: str, endpoint: str, query: dict) -> tuple[str, str]:
    """``(kind, table)``: the request named, not yet understood (an unfamiliar table is recorded, not rejected)."""
    path = (endpoint or "").lower()
    table = query.get("table")
    if isinstance(table, list):
        table = table[0] if table else ""
    table = str(table or "").strip().upper()[:40]
    for name, kind in (("getrequest", "GETREQUEST"), ("devicecmd", "DEVICECMD"), ("registry", "REGISTRY"), ("ping", "PING")):
        if name in path:
            return kind, table
    if "cdata" in path:
        if method.upper() == "GET" or "options" in query or "option" in query:
            return "HANDSHAKE", table
        if table in TABLE_KINDS:
            return table, table
        return "CDATA", table
    return "UNKNOWN", table


def safe_headers(headers: dict) -> dict:
    return {scrub(str(key)): scrub(str(value))[:1000] for key, value in headers.items() if str(key).lower() not in DROPPED_HEADERS}


# --------------------------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------------------------
def parse_attlog(text: str) -> tuple[list[dict], int]:
    """``PIN<TAB>time<TAB>status<TAB>verify<TAB>workcode…`` → punches. Returns ``(records, unparsable_lines)``.

    Column mapping (eSSL, UNPROVEN until the controlled one-terminal test, D-10): the ATTLOG *verify* field goes to
    ``status`` and the ATTLOG *status* field to ``punch``, one meaning per column across both transports; every field
    is also kept verbatim in ``raw_payload``.
    """
    records, bad = [], 0
    for line in text.splitlines():
        line = scrub(line)
        if not line.strip():
            continue
        fields = [part.strip() for part in (line.split("\t") if "\t" in line else re.split(r"\s{2,}", line))]
        if len(fields) < 2 or not fields[0] or ingest.parse_device_time(fields[1]) is None:
            bad += 1
            continue
        payload = {
            "_transport": ingest.ADMS_PUSH,
            "_format": "ATTLOG",
            "_line": line,
            "_column_mapping": "attlog.verify->status, attlog.status->punch (UNPROVEN)",
            "attlog_status": fields[2] if len(fields) > 2 else None,
            "attlog_verify": fields[3] if len(fields) > 3 else None,
            "attlog_workcode": fields[4] if len(fields) > 4 else None,
        }
        if len(fields) > 5:
            payload["attlog_reserved"] = fields[5:]
        records.append({"pin": fields[0], "device_time": fields[1], "status": fields[3] if len(fields) > 3 else None, "punch": fields[2] if len(fields) > 2 else None, "raw_payload": payload})
    return records, bad


def parse_kv_lines(text: str) -> list[dict[str, str]]:
    """``[PREFIX ]KEY=value<TAB>KEY=value`` lines (USERINFO, OPERLOG); unknown keys are kept."""
    rows = []
    for line in text.splitlines():
        line = scrub(line).strip()
        if not line:
            continue
        row: dict[str, str] = {}
        for index, part in enumerate(re.split(r"\t+", line)):
            if "=" in part:
                key, _, value = part.partition("=")
                key = key.strip()
                if index == 0 and " " in key:  # "USER PIN=1": the record type leads the line
                    prefix, _, key = key.partition(" ")
                    row["_prefix"] = prefix.strip()
                    key = key.strip()
                row[key] = value.strip()
            elif part.strip():
                row.setdefault("_prefix", part.strip())
        if row:
            rows.append(row)
    return rows


def redact_secrets(text: str) -> tuple[str, bool]:
    """Terminal-user passwords and biometric templates (``Passwd=…``, ``TMP=…``) replaced by ``***``."""
    changed = False

    def replace(match):
        nonlocal changed
        if match.group(2):
            changed = True
            return f"{match.group(1)}***"
        return match.group(0)

    pattern = re.compile(r"((?:^|[\t ])(?:" + "|".join(SECRET_KEYS) + r")=)([^\t\r\n]*)", re.IGNORECASE | re.MULTILINE)
    return pattern.sub(replace, text), changed


def user_rows(rows: list[dict], table: str) -> list[dict]:
    """The rows that describe a terminal user (USERINFO lines, OPERLOG ``USER`` lines) as agent-style entries."""
    users = []
    for row in rows:
        prefix = row.get("_prefix", "").upper()
        if "PIN" not in row or (table != "USERINFO" and prefix != "USER"):
            continue
        privilege = row.get("Pri")
        users.append(
            {
                "pin": row.get("PIN", ""),
                "name": row.get("Name", ""),
                "privilege": int(privilege) if privilege and privilege.lstrip("-").isdigit() else None,
                "card": row.get("Card", ""),
                "group_id": row.get("Grp", ""),
                "has_password": bool(row.get("Passwd")),
                "raw_payload": {key: ("***" if key.lower() in SECRET_KEYS else value) for key, value in row.items()},
            }
        )
    return users


# --------------------------------------------------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------------------------------------------------
def handshake_options(serial: str) -> str:
    """The option block. No TimeZone (the terminal's clock is never set); TransFlag limited to AttLog and OpLog."""
    return (
        f"GET OPTION FROM: {serial}\r\n"
        "Stamp=9999\r\n"
        "OpStamp=9999\r\n"
        f"ErrorDelay={int(setting('DEVICES_ADMS_ERROR_DELAY_SECONDS', 30))}\r\n"
        f"Delay={int(setting('DEVICES_ADMS_POLL_DELAY_SECONDS', 10))}\r\n"
        "TransTimes=00:00;14:05\r\n"
        f"TransInterval={int(setting('DEVICES_ADMS_TRANSACTION_INTERVAL_MINUTES', 1))}\r\n"
        "TransFlag=AttLog OpLog\r\n"
        f"Realtime={1 if setting('DEVICES_ADMS_REALTIME', True) else 0}\r\n"
        "Encrypt=0\r\n"
    )


# --------------------------------------------------------------------------------------------------------------------
# Identity, admission, quarantine, contact
# --------------------------------------------------------------------------------------------------------------------
def serial_belongs(device: Device, serial: str) -> bool:
    if device.serial_number:
        return device.serial_number == serial
    return bool(device.expected_serial) and device.expected_serial == serial


def quarantine(serial: str, reason: str, incoming: Incoming, excerpt: str) -> None:
    at = now()
    values = {"last_seen_at": at, "last_source_ip": incoming.client_ip, "last_path": incoming.path[:255], "last_body_excerpt": scrub(excerpt[:BODY_EXCERPT_CHARS]), "last_reason": reason}
    updated = AdmsUnknownDevice.objects.filter(serial_number=serial).update(request_count=F("request_count") + 1, **values)
    if updated:
        return
    try:
        with transaction.atomic():
            AdmsUnknownDevice.objects.create(serial_number=serial, first_seen_at=at, request_count=1, **values)
    except IntegrityError:  # a concurrent first request created it
        AdmsUnknownDevice.objects.filter(serial_number=serial).update(request_count=F("request_count") + 1, **values)


def touch(device: Device, kind: str, source_ip: str | None) -> None:
    """Contact from the terminal (the only input of its push health). A malformed push still proves it is alive."""
    at = now()
    values: dict[str, Any] = {"adms_last_seen_at": at, "adms_source_ip": source_ip, "adms_request_count": F("adms_request_count") + 1}
    if kind == "HANDSHAKE":
        values.update(adms_last_handshake_at=at, adms_registration_state=Device.RegistrationState.REGISTERED)
    elif kind == "GETREQUEST":
        values["adms_last_command_poll_at"] = at
    elif kind in ("ATTLOG", "OPERLOG", "USERINFO", "CDATA"):
        values["adms_last_push_at"] = at
    Device.all_objects.filter(pk=device.pk).update(**values)


def refusal(device: Device, client_ip: str | None) -> str:
    if not device.is_active:
        return "The device is deactivated on the platform; nothing ingested."
    if not device.adms_enabled:
        return "ADMS is not enabled for this device; nothing ingested."
    if not ip_allowed(client_ip, device.adms_allowed_ips):
        return f"Source address {client_ip or 'unknown'} is not in the device's allow-list; nothing ingested."
    return ""


# --------------------------------------------------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------------------------------------------------
def _interpret(device: Device, kind: str, table: str, serial: str, incoming: Incoming, text: str, request_id: int, outcome: Outcome) -> None:
    if kind == "HANDSHAKE":
        outcome.reply = handshake_options(serial)
        values: dict[str, Any] = {"adms_options": scrub_deep({"query": flatten_query(incoming.query), "requested_at": now().isoformat()})}
        if not device.serial_number:  # first contact of a device registered from its label: it stated its own serial
            values.update(
                serial_number=serial, identity_status=Device.IdentityStatus.VERIFIED, identity_checked_at=now(), identity_message=f"Serial {serial} confirmed by the terminal itself over ADMS."
            )
        Device.all_objects.filter(pk=device.pk).update(**values)
    elif kind == "ATTLOG":
        records, bad = parse_attlog(text)
        counts = ingest.ingest(device, records, source=ingest.ADMS_PUSH, adms_request_id=request_id)
        outcome.records = {"parsed": counts["received"], "new": counts["new"], "duplicate": counts["duplicate"], "invalid": counts["invalid"] + bad}
        outcome.extra["discarded"] = counts["discarded"]
        outcome.reply = f"OK: {counts['received']}"
    elif kind in ("USERINFO", "OPERLOG"):
        rows = parse_kv_lines(text)
        users = user_rows(rows, kind)
        applied = 0
        if users:
            created, updated, invalid, present = agent_protocol.upsert_users(device, users, at=now())
            applied = created + updated
            outcome.records["invalid"] = invalid
            bump_devices(NS_DEVICE_USERS)
        outcome.records["parsed"] = len(rows)
        outcome.extra.update(parsed=[{key: ("***" if key.lower() in SECRET_KEYS else value) for key, value in row.items()} for row in rows[:200]], users_applied=applied)
        outcome.reply = f"OK: {len(rows)}"
    elif kind in BIOMETRIC_KINDS:
        outcome.store_body = False
        outcome.extra["note"] = "biometric payload: not stored and not applied (the handshake never asks for it)"
    elif kind == "DEVICECMD":
        outcome.extra["note"] = "command result received; this server issues no commands"


def _decide(incoming: Incoming, kind: str, table: str, serial: str | None, text: str, request_id: int) -> Outcome:
    outcome = Outcome()
    device = device_for_token(incoming.token)
    if serial is None:
        outcome.parse_error = "No serial in the request; it cannot be attributed to a device."
        return outcome
    if device is None or not serial_belongs(device, serial):
        registered = Device.objects.filter(serial_number=serial).exists() or Device.objects.filter(expected_serial=serial).exists()
        reason = AdmsUnknownDevice.Reason.TOKEN_MISMATCH if (device is not None or registered) else AdmsUnknownDevice.Reason.UNKNOWN_SERIAL
        quarantine(serial, reason, incoming, text)
        outcome.parse_error = f"Serial {serial} and the device token do not belong together ({reason}). Quarantined; nothing ingested."
        return outcome
    outcome.device = device
    refused = refusal(device, incoming.client_ip)
    if refused:
        outcome.parse_error = refused
        return outcome
    touch(device, kind, incoming.client_ip)
    try:
        with transaction.atomic():
            _interpret(device, kind, table, serial, incoming, text, request_id, outcome)
    except Exception as exc:  # noqa: BLE001 - a parser or store failure must never cost the evidence
        logger.exception("ADMS interpretation failed", extra={"device": str(device.uid), "request_kind": kind})
        outcome.parse_error = f"{type(exc).__name__}: {exc}"[:2000]
        outcome.reply = ERROR  # not acknowledged: the terminal resends
    return outcome


def _next_id() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('devices_adms_request_id_seq')")
        return cursor.fetchone()[0]


def _store(incoming: Incoming, *, request_id: int, kind: str, table: str, serial: str | None, text: str, encoding: str, outcome: Outcome) -> None:
    body = outcome.body_override if outcome.body_override is not None else incoming.body
    AdmsRequest.objects.create(
        id=request_id,
        received_at=now(),
        client_ip=incoming.client_ip,
        peer_ip=incoming.peer_ip,
        method=incoming.method.upper()[:10],
        path=incoming.path[:255],
        raw_query=scrub(incoming.raw_query)[:4000],
        query=scrub_deep(flatten_query(incoming.query)),
        headers=safe_headers(incoming.headers),
        content_type=scrub(incoming.content_type)[:160],
        device_serial=serial or "",
        device=outcome.device,
        request_kind=kind,
        table_name=table,
        body=(body[:MAX_STORED_BODY_BYTES] or None) if outcome.store_body else None,
        body_bytes=incoming.body_bytes,
        body_text=scrub(text[:BODY_EXCERPT_CHARS]) if outcome.store_body else "",
        body_encoding=encoding,
        body_truncated=incoming.truncated or len(body) > MAX_STORED_BODY_BYTES,
        response_status=200,
        response_body=outcome.reply[:4000],
        records_parsed=outcome.records["parsed"],
        records_new=outcome.records["new"],
        records_duplicate=outcome.records["duplicate"],
        records_invalid=outcome.records["invalid"],
        parse_error=outcome.parse_error,
        extra=scrub_deep(outcome.extra),
    )


def receive(incoming: Incoming) -> str:
    """Handle one terminal request and return the text reply (always sent with HTTP 200)."""
    try:
        with transaction.atomic():
            request_id = _next_id()
            kind, table = classify(incoming.method, incoming.endpoint, incoming.query)
            serial = extract_serial(incoming.query)
            text, encoding = decode_best_effort(incoming.body) if incoming.body else ("", "")
            redacted = False
            if kind in ("USERINFO", "OPERLOG", "CDATA", "UNKNOWN") and text:
                text, redacted = redact_secrets(text)
            outcome = _decide(incoming, kind, table, serial, text, request_id)
            if redacted:
                outcome.body_override = text.encode(encoding if encoding in DECODE_CANDIDATES else "utf-8", errors="replace")
                outcome.extra["redacted"] = "terminal-user passwords and biometric templates"
            _store(incoming, request_id=request_id, kind=kind, table=table, serial=serial, text=text, encoding=encoding, outcome=outcome)
            return outcome.reply
    except Exception as exc:  # noqa: BLE001 - the receiver must stay up for the other offices
        logger.exception("ADMS request could not be recorded")
        try:
            with transaction.atomic():
                AdmsRequest.objects.create(
                    id=_next_id(),
                    received_at=now(),
                    client_ip=incoming.client_ip,
                    peer_ip=incoming.peer_ip,
                    method=incoming.method.upper()[:10],
                    path=incoming.path[:255],
                    request_kind=AdmsRequest.Kind.UNSTORABLE,
                    body_bytes=incoming.body_bytes,
                    response_body=ERROR,
                    parse_error=f"could not record this request: {type(exc).__name__}"[:2000],
                )
        except Exception:  # noqa: BLE001
            logger.exception("ADMS request could not be recorded even minimally")
        return ERROR
