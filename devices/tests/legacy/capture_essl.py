"""Build the eSSL legacy fixtures of the devices package (run by hand, never by pytest).

The fixtures are produced by the eSSL application itself — no hand-written rows:

1. a **private** Postgres database (``essl_wp_devices``) is migrated with the eSSL app's own alembic chain and seeded
   with its own ``scripts/seed.py`` (roles, ``admin``, offices HO/BR1/BR2, shifts GEN/NIGHT, device MARS-01);
2. a private eSSL server (uvicorn on 18152, ``ADMS_ENABLED=true``) is driven through its **own endpoints** only:
   staff API (employees, agents with tokens, devices registered by address and from the label, a pushing terminal, a
   deactivated terminal, links, ``map-pin`` — which links every row with the PIN on every device, the A1 case —,
   ``auto-link``, protocol mappings), the **agent protocol** with the issued tokens (heartbeat, announce, users and
   attendance uploads, a LAN discovery, an identity-mismatch report, a second users read that drops a PIN) and the
   **ADMS receiver** (``/iclock/cdata`` handshake, ATTLOG with a malformed line, OPERLOG with USER lines, command poll,
   device command result, ping, an unknown serial);
3. every table is exported read-only (``SELECT *``) to ``essl_devices_tables.json``, the legacy API's GET responses
   to ``essl_devices_api.json`` and every ADMS request with the receiver's exact reply (status, headers, body) to
   ``essl_adms_exchanges.json``. People's names, e-mails and phones are masked deterministically; the agents' bcrypt
   token hashes are dropped (the platform issues new credentials anyway).

Usage (see docs/decisions/devices.md "Parity evidence")::

    createdb essl_wp_devices
    cd <copy of essl-webap-main/backend>
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/essl_wp_devices /home/user/.venvs/essl/bin/alembic upgrade head
    DATABASE_URL=... /home/user/.venvs/essl/bin/python -m scripts.seed
    DATABASE_URL=... ENVIRONMENT=local ADMS_ENABLED=true /home/user/.venvs/essl/bin/uvicorn app.main:app --port 18152 &
    export ESSL_ADMIN_PASSWORD=<the seed admin password>
    /home/user/.venvs/platform/bin/python devices/tests/legacy/capture_essl.py --base http://127.0.0.1:18152 \
        --dsn postgresql://postgres:postgres@localhost:5432/essl_wp_devices
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
import re
from pathlib import Path

import psycopg
import requests

HERE = Path(__file__).resolve().parent
FORBIDDEN_DATABASES = {"legacy_goldenapp", "legacy_blog_cms", "essl_attendance"}
HR_TABLES = ["roles", "users", "offices", "shifts", "employees"]
DEVICE_TABLES = ["agents", "devices", "device_users", "sync_logs", "protocol_mappings", "adms_unknown_devices", "adms_requests"]

MARS = {"serial": "NCD8253601138", "mac": "00:17:61:12:9c:49", "ip": "192.168.1.209"}
SALES = {"serial": "NCD8252101212", "mac": "00:17:61:12:f2:d1", "ip": "192.168.1.60"}
PROJECT = {"serial": "NCD8252101398", "mac": "00:17:61:12:14:c8"}
UNKNOWN_SERIAL = "ZZZ0000000001"


class Legacy:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        token = self.session.post(f"{self.base}/api/auth/login", json={"username": "admin", "password": os.environ["ESSL_ADMIN_PASSWORD"]}, timeout=10).json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, *, expect=(200, 201), **kwargs):
        response = self.session.request(method, f"{self.base}{path}", timeout=30, **kwargs)
        if response.status_code not in expect:
            raise SystemExit(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json()

    def get(self, path: str, **params):
        return self.call("GET", path, params=params)

    def agent(self, token: str, method: str, path: str, *, expect=(200,), **kwargs):
        response = requests.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kwargs)
        if response.status_code not in expect:
            raise SystemExit(f"agent {method} {path} -> {response.status_code}: {response.text}")
        return response.json()


def iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def populate(api: Legacy) -> list[dict]:
    offices = {row["code"]: row["id"] for row in api.get("/api/offices")}
    if api.get("/api/agents"):
        raise SystemExit("already populated: restore a fresh seeded database first")
    ho, br1, br2 = offices["HO"], offices["BR1"], offices["BR2"]

    # --- employees ------------------------------------------------------------------------------------------------
    def employee(**body):
        return api.call("POST", "/api/employees", json=body)

    asha = employee(employee_code="E001", full_name="Asha Menon", office_id=ho)
    binu = employee(employee_code="E002", full_name="Binu Joseph", office_id=ho)
    chitra = employee(employee_code="E003", full_name="Chitra Nair", office_id=br1)
    employee(employee_code="3", full_name="Deepak Kumar", office_id=ho)  # a code equal to a PIN (auto-link)
    employee(employee_code="E005", full_name="Elena Varghese", office_id=br1)

    # --- agents (tokens shown once) ---------------------------------------------------------------------------------
    a1 = api.call("POST", "/api/agents", json={"code": "OFFICE-001-AGENT", "name": "Head office PC", "office_id": ho, "notes": "Reception desktop"})
    a2 = api.call("POST", "/api/agents", json={"code": "OFFICE-002-AGENT", "name": "Sales office PC", "office_id": br1, "heartbeat_interval_seconds": 120, "offline_after_seconds": 600})
    a3 = api.call("POST", "/api/agents", json={"code": "OFFICE-003-AGENT", "name": "Project office PC", "office_id": br2})
    api.call("POST", f"/api/agents/{a3['agent_id']}/revoke")
    t1, t2 = a1["token"], a2["token"]

    # --- devices ----------------------------------------------------------------------------------------------------
    mars = next(row for row in api.get("/api/devices") if row["name"] == "MARS-01")
    api.call("PATCH", f"/api/devices/{mars['id']}", json={"agent_id": a1["agent_id"], "expected_serial": MARS["serial"], "expected_mac": MARS["mac"]})
    sales = api.call(
        "POST",
        "/api/devices",
        json={"name": "SALES-01", "expected_serial": SALES["serial"], "expected_mac": SALES["mac"], "office_id": br1, "agent_id": a2["agent_id"], "notes": "Registered from the hardware label"},
    )
    api.call(
        "POST",
        "/api/devices",
        json={"name": "PROJECT-01", "expected_serial": PROJECT["serial"], "expected_mac": PROJECT["mac"], "office_id": br2, "adms_enabled": True, "adms_server": "117.247.191.140", "adms_port": 8080},
    )
    api.call("POST", "/api/devices", json={"name": "MARS-02", "ip_address": "192.168.1.210", "expected_serial": "NCD0000000001", "office_id": ho, "agent_id": a1["agent_id"]})
    old = api.call("POST", "/api/devices", json={"name": "OLD-TERMINAL", "ip_address": "192.168.1.50", "office_id": ho, "agent_id": a1["agent_id"], "protocol": "ZK_UDP", "comm_password": 1234})
    api.call("PATCH", f"/api/devices/{old['id']}", json={"is_active": False})

    # --- agent 1: heartbeat, announce, users, attendance ---------------------------------------------------------------
    api.agent(t1, "POST", "/api/agent/heartbeat", json={"version": "1.0.0", "hostname": "HO-PC", "platform": "Windows-10", "local_ip": "192.168.1.20", "devices": []})
    announced = api.agent(
        t1,
        "POST",
        "/api/agent/devices/announce",
        json={
            "serial_number": MARS["serial"],
            "name": "MARS-01",
            "ip_address": MARS["ip"],
            "port": 4370,
            "model": "x 2008",
            "firmware_version": "Ver 6.60 Aug 19 2021",
            "platform": "ZAM180_TFT",
            "mac_address": MARS["mac"],
            "device_info": {"device_name": "x 2008", "unavailable": {}},
        },
    )
    users_mars = [
        {"device_user_id": "1", "device_uid": 1, "name": "Asha", "privilege": 14, "card": "", "group_id": "1", "has_password": False, "raw_payload": {"uid": 1, "user_id": "1", "name": "Asha"}},
        {"device_user_id": "2", "device_uid": 2, "name": "Binu", "privilege": 0, "card": "5541202", "group_id": "1", "has_password": True, "raw_payload": {"uid": 2, "user_id": "2", "name": "Binu"}},
        {"device_user_id": "3", "device_uid": 3, "name": "Deepak", "privilege": 0, "card": "", "group_id": "1", "has_password": False, "raw_payload": {"uid": 3, "user_id": "3", "name": "Deepak"}},
        {"device_user_id": "EMP004", "device_uid": 4, "name": "Visitor", "privilege": 0, "card": "", "group_id": "", "has_password": False, "raw_payload": {"uid": 4, "user_id": "EMP004"}},
        {"device_user_id": "9", "device_uid": 9, "name": "", "privilege": 0, "card": "", "group_id": "", "has_password": False, "raw_payload": {"uid": 9, "user_id": "9"}},
    ]
    api.agent(t1, "POST", "/api/agent/sync/users", json={"device_id": announced["device_id"], "users": users_mars})
    base = dt.datetime(2026, 9, 21, 9, 28, 11)
    punches = [
        {"device_record_uid": 101, "device_user_id": "1", "punch_time": iso(base), "status": 15, "punch": 255, "raw_payload": {"uid": 101}},
        {"device_record_uid": 102, "device_user_id": "2", "punch_time": iso(base + dt.timedelta(minutes=4)), "status": 15, "punch": 255, "raw_payload": {"uid": 102}},
        {"device_record_uid": 103, "device_user_id": "1", "punch_time": iso(base + dt.timedelta(hours=9, minutes=2)), "status": 4, "punch": 0, "raw_payload": {"uid": 103}},
    ]
    api.agent(t1, "POST", "/api/agent/sync/attendance", json={"device_id": announced["device_id"], "batch_id": "capture-1", "records": punches})
    api.agent(
        t1,
        "POST",
        "/api/agent/heartbeat",
        json={"version": "1.0.0", "hostname": "HO-PC", "platform": "Windows-10", "local_ip": "192.168.1.20", "queued_records": 0, "devices": [{"serial_number": MARS["serial"], "is_online": True}]},
    )
    api.agent(
        t1,
        "POST",
        "/api/agent/devices/identity-mismatch",
        json={"expected_serial": "NCD0000000001", "reported_serial": "NCD9999999999", "ip_address": "192.168.1.210", "port": 4370, "reason": "a different terminal answered at this address"},
    )

    # --- agent 2: discovery locates the label-registered terminal, then users twice (a PIN leaves) --------------------
    api.agent(t2, "POST", "/api/agent/heartbeat", json={"version": "1.0.0", "hostname": "SALES-PC", "platform": "Windows-11", "local_ip": "192.168.1.21", "queued_records": 612, "devices": []})
    api.agent(
        t2,
        "POST",
        "/api/agent/devices/discovery",
        json={
            "subnet": "192.168.1.0/24",
            "agent_ip": "192.168.1.21",
            "gateway": "192.168.1.1",
            "hosts_scanned": 254,
            "hosts_open": 2,
            "found": [
                {
                    "ip_address": SALES["ip"],
                    "port": 4370,
                    "mac_address": SALES["mac"],
                    "serial_number": SALES["serial"],
                    "device_name": "x 2008",
                    "firmware_version": "Ver 6.60 Aug 19 2021",
                    "platform": "ZAM180_TFT",
                },
                {"ip_address": "192.168.1.203", "port": 4370, "error": "TimeoutError: timed out"},
            ],
        },
    )
    users_sales = [
        {"device_user_id": "1", "device_uid": 1, "name": "Chitra", "privilege": 0, "raw_payload": {"uid": 1, "user_id": "1"}},
        {"device_user_id": "2", "device_uid": 2, "name": "Binu", "privilege": 0, "raw_payload": {"uid": 2, "user_id": "2"}},
        {"device_user_id": "5", "device_uid": 5, "name": "Elena", "privilege": 0, "raw_payload": {"uid": 5, "user_id": "5"}},
    ]
    api.agent(t2, "POST", "/api/agent/sync/users", json={"serial_number": SALES["serial"], "users": users_sales})
    api.agent(t2, "POST", "/api/agent/sync/users", json={"serial_number": SALES["serial"], "users": users_sales[:2]})  # PIN 5 left the terminal

    # --- links ------------------------------------------------------------------------------------------------------------
    rows = {(row["device_id"], row["device_user_id"]): row["id"] for row in api.get("/api/device-users")}
    api.call("POST", f"/api/device-users/{rows[(announced['device_id'], '1')]}/link", json={"employee_id": asha["id"]})
    api.call("POST", f"/api/device-users/{rows[(sales['id'], '1')]}/link", json={"employee_id": chitra["id"]})  # the same PIN, another person
    api.call("POST", "/api/device-users/map-pin", json={"device_user_id": "2", "employee_id": binu["id"]})  # every row with PIN 2, on every device
    api.call("POST", "/api/device-users/auto-link")  # PIN 3 == employee code 3

    # --- protocol mappings ----------------------------------------------------------------------------------------------
    api.call(
        "POST",
        "/api/protocol-mappings",
        json={
            "device_platform": "ZAM180_TFT",
            "firmware_version": "Ver 6.60 Aug 19 2021",
            "field": "status",
            "raw_value": 15,
            "meaning_type": "VERIFY_MODE",
            "meaning_code": "FACE",
            "label": "Face",
            "confidence": "ASSUMED",
            "notes": "493 punches, all face",
        },
    )
    api.call("POST", "/api/protocol-mappings", json={"field": "status", "raw_value": 4, "meaning_type": "VERIFY_MODE", "meaning_code": "CARD", "label": "Card", "confidence": "UNKNOWN"})
    api.call(
        "POST",
        "/api/protocol-mappings",
        json={
            "device_platform": "ZAM180_TFT",
            "field": "punch",
            "raw_value": 255,
            "meaning_type": "PUNCH_DIRECTION",
            "meaning_code": "UNSPECIFIED",
            "label": "No punch state",
            "confidence": "VERIFIED",
        },
    )

    # --- ADMS receiver --------------------------------------------------------------------------------------------------
    exchanges = []
    serial = PROJECT["serial"]
    attlog = "7\t2026-09-22 09:31:05\t0\t15\t0\t0\n8\t2026-09-22 09:40:44\t0\t1\t\t0\nnot a punch line\n"
    operlog = "USER PIN=7\tName=Ravi\tPri=0\tPasswd=4321\tCard=[000]\tGrp=1\tTZ=0000000100000000\tVerify=0\nOPLOG 4\t0\t2026-09-22 09:35:00\t0\t0\t0\t0\n"
    for method, path, query, body in [
        ("GET", "/iclock/cdata", {"SN": serial, "options": "all", "pushver": "2.4.1", "language": "69"}, None),
        ("POST", "/iclock/cdata", {"SN": serial, "table": "ATTLOG", "Stamp": "9999"}, attlog),
        ("POST", "/iclock/cdata", {"SN": serial, "table": "ATTLOG", "Stamp": "9999"}, attlog),  # resent: nothing new
        ("POST", "/iclock/cdata", {"SN": serial, "table": "OPERLOG", "OpStamp": "9999"}, operlog),
        ("GET", "/iclock/getrequest", {"SN": serial}, None),
        ("POST", "/iclock/devicecmd", {"SN": serial}, "ID=1&Return=0&CMD=INFO\n"),
        ("GET", "/iclock/ping", {"SN": serial}, None),
        ("GET", "/iclock/registry", {"SN": serial}, None),
        ("GET", "/iclock/cdata", {"SN": UNKNOWN_SERIAL, "options": "all"}, None),
        ("POST", "/iclock/cdata", {"SN": UNKNOWN_SERIAL, "table": "ATTLOG"}, "1\t2026-09-22 10:00:00\t0\t15\n"),
        ("GET", "/iclock/cdata", {"options": "all"}, None),
    ]:
        response = requests.request(method, f"{api.base}{path}", params=query, data=body.encode() if body else None, headers={"Content-Type": "text/plain"} if body else {}, timeout=30)
        exchanges.append(
            {
                "request": {"method": method, "path": path, "query": query, "body": body},
                "response": {
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "content_length": response.headers.get("content-length"),
                    "body": response.text,
                },
            }
        )
    print("populated through the legacy API, agent protocol and ADMS receiver")
    return exchanges


# --- masking -------------------------------------------------------------------------------------------------------
def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mask_email(value):
    if not value or "@" not in value:
        return value
    return f"person-{_digest(value.lower())[:8]}@example.test"


def mask_name(value):
    if not value:
        return value
    return f"Person {_digest(value)[:6].upper()}"


PERSON_KEYS = {"full_name", "employee_name", "device_user_name", "Name"}
# "name" is a person's name only on terminal-user shapes (device users, their raw payloads, per-device mappings);
# on devices and agents it is the equipment's name and stays readable.
PERSON_MARKERS = {"device_user_id", "user_id", "PIN", "employee_code"}


def mask_payload(payload):
    if isinstance(payload, dict):
        person = bool(PERSON_MARKERS & set(payload))
        return {key: (mask_name(value) if isinstance(value, str) and (key in PERSON_KEYS or (key == "name" and person)) else mask_payload(value)) for key, value in payload.items()}
    if isinstance(payload, list):
        return [mask_payload(item) for item in payload]
    return payload


def masked_tables(dsn: str) -> dict:
    tables = {}
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for table in HR_TABLES + DEVICE_TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")  # read-only export
            tables[table] = [dict(row) for row in cur.fetchall()]
    for row in tables["users"]:
        row["email"] = mask_email(row["email"])
        row["full_name"] = mask_name(row["full_name"])
        row["password_hash"] = "<dropped: not needed by the devices import>"
    for row in tables["employees"]:
        row["email"] = mask_email(row["email"])
        row["phone"] = None
        row["full_name"] = mask_name(row["full_name"])
    for row in tables["agents"]:
        row["token_hash"] = "<dropped: bcrypt hash of a capture-only token>"
    for row in tables["device_users"]:
        row["name"] = mask_name(row["name"])
        row["raw_payload"] = mask_payload(row["raw_payload"])
    for row in tables["adms_requests"]:
        # terminal-user names inside pushed USER lines; the capture's terminal password (a made-up 4321) is kept so the
        # import test can prove the platform redacts it
        for field in ("body", "body_text"):
            if row[field] is not None:
                text = bytes(row[field]).decode("utf-8", errors="replace") if isinstance(row[field], (bytes, memoryview)) else row[field]
                row[field] = re.sub(r"(Name=)([^\t\r\n]*)", lambda match: match.group(1) + (mask_name(match.group(2)) or ""), text)
        row["extra"] = mask_payload(row["extra"])
    return tables


def masked_api(api: Legacy) -> dict:
    devices = api.get("/api/devices")
    by_name = {row["name"]: row["id"] for row in devices}
    employees = api.get("/api/employees", presence="ALL", include_inactive="true", page_size=100)["items"]
    data = {
        "devices": devices,
        "agents": api.get("/api/agents"),
        "device_users": api.get("/api/device-users"),
        "protocol_mappings": api.get("/api/protocol-mappings"),
        "mapping": api.get("/api/devices/mapping"),
        "user_reconciliation": {name: api.get(f"/api/devices/{by_name[name]}/user-reconciliation") for name in ("MARS-01", "SALES-01")},
        "employee_reconciliation": {name: api.get(f"/api/devices/{by_name[name]}/employee-reconciliation") for name in ("MARS-01", "SALES-01")},
        "device_mappings": {row["employee_code"]: api.get(f"/api/employees/{row['id']}/device-mappings") for row in employees},
        "adms_unknown_devices": api.get("/api/adms/unknown-devices"),
    }
    return mask_payload(json.loads(json.dumps(data)))


def _default(value):
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    raise TypeError(type(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="private eSSL API, e.g. http://127.0.0.1:18152")
    parser.add_argument("--dsn", required=True, help="the private eSSL database")
    parser.add_argument("--export-only", action="store_true", help="only re-export the tables and the GET responses (no writes)")
    args = parser.parse_args()
    database = args.dsn.rsplit("/", 1)[-1].split("?")[0]
    if database in FORBIDDEN_DATABASES:
        raise SystemExit(f"refusing to write to {database}: use a private copy")
    api = Legacy(args.base)
    if args.export_only:  # read-only: re-export the tables and the API responses of an already populated instance
        (HERE / "essl_devices_tables.json").write_text(json.dumps(masked_tables(args.dsn), indent=1, default=_default, sort_keys=True) + "\n")
        (HERE / "essl_devices_api.json").write_text(json.dumps(masked_api(api), indent=1, default=_default, sort_keys=True) + "\n")
        print("wrote essl_devices_tables.json and essl_devices_api.json")
        return 0
    exchanges = populate(api)
    (HERE / "essl_adms_exchanges.json").write_text(json.dumps(exchanges, indent=1, sort_keys=True) + "\n")
    (HERE / "essl_devices_tables.json").write_text(json.dumps(masked_tables(args.dsn), indent=1, default=_default, sort_keys=True) + "\n")
    (HERE / "essl_devices_api.json").write_text(json.dumps(masked_api(api), indent=1, default=_default, sort_keys=True) + "\n")
    print("wrote essl_adms_exchanges.json, essl_devices_tables.json and essl_devices_api.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
