"""Build the eSSL legacy fixtures of the attendance package (run by hand, never by pytest).

The fixtures are produced by the eSSL application itself — no hand-written attendance rows:

1. a **private** copy of a seeded eSSL database (``essl_wp_attendance``: eSSL's alembic chain + ``scripts/seed.py`` —
   roles, admin, offices HO/BR1/BR2, shifts GEN/NIGHT, device MARS-01), its admin password set for the capture;
2. a private eSSL server (uvicorn on 127.0.0.1:18153, ``ADMS_ENABLED=true``) driven through its **own endpoints**:
   staff API (shift, employees, agent, a second terminal, holidays, leave, ``map-pin`` without a device — which links
   the PIN on every terminal, the A1 case), the **agent protocol** (announce, users, punches for August 2026), the
   **ADMS receiver** (an ATTLOG push of a punch the agent delivers again: eSSL stores it twice under two keys), and
   finally ``POST /api/attendance/process`` for August — the v3 engine's own stored days;
3. every table is exported read-only (``SELECT *``) to ``essl_attendance_tables.json``. People's names are masked
   deterministically (``Person <sha256[:6]>``), e-mails/phones dropped, password and agent token hashes dropped.

Scenarios (August 2026, GEN 09:30–18:30 unless stated) — each is an A-row of PLAN §2.9:

* E001 on MARS-01 PIN 1: present, late, half day, a double scan (A3), a 61-minute day (A2), absent, full-day leave
  with punches (A4 leave_conflict), a worked holiday (15th, A4), a worked Sunday (16th, A4), a half-day leave;
* E002 on shift LATE 11:00–20:00: an arrival at 11:40 short of a full day (A5: v3's 10:00 wall clock never fires);
* E003 on NIGHT 22:00–06:00: an OUT at 06:45 inside the overnight buffer (A6);
* E004 PIN 4 on MARS-01; PIN 4 on MARS-02 is a visitor nobody linked there — ``map-pin`` without a device linked it
  everywhere in eSSL, and the global PIN map counted the visitor's punches as E004's (A1);
* E005 with code ``5`` and PIN 5 punching unlinked: eSSL's ``employee_code`` fallback (A1);
* an ATTLOG push and an agent upload of the same punch (A11: one row after the import).

Usage (see docs/decisions/attendance.md "Parity evidence")::

    createdb essl_wp_attendance && pg_restore --no-owner -d essl_wp_attendance <seeded eSSL dump>
    # set users.password_hash of admin to a bcrypt hash of $ESSL_ADMIN_PASSWORD (app.core.security.hash_password)
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/essl_wp_attendance ENVIRONMENT=local ADMS_ENABLED=true \\
        /home/user/.venvs/essl/bin/uvicorn app.main:app --port 18153 &
    /home/user/.venvs/platform/bin/python attendance/tests/legacy/capture_essl.py --base http://127.0.0.1:18153 \\
        --dsn postgresql://postgres:postgres@localhost:5432/essl_wp_attendance
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path

import psycopg
import requests

HERE = Path(__file__).resolve().parent
FORBIDDEN_DATABASES = {"legacy_goldenapp", "legacy_blog_cms", "essl_attendance"}
TABLES = ["roles", "users", "offices", "shifts", "employees", "holidays", "leave_records", "attendance_rules", "agents", "devices", "device_users", "attendance_raw", "attendance"]
DROP = {"users": {"password_hash"}, "agents": {"token_hash"}, "employees": {"email", "phone", "photo_url"}}
MASK_NAMES = {"employees": "full_name", "users": "full_name", "device_users": "name"}
MARS_SERIAL, MARS2_SERIAL = "NCD8253601138", "NCD8253601139"


class Legacy:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        token = self.session.post(f"{self.base}/api/auth/login", json={"username": "admin", "password": os.environ["ESSL_ADMIN_PASSWORD"]}, timeout=10).json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, *, expect=(200, 201), **kwargs):
        response = self.session.request(method, f"{self.base}{path}", timeout=60, **kwargs)
        if response.status_code not in expect:
            raise SystemExit(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json()

    def get(self, path: str, **params):
        return self.call("GET", path, params=params)

    def agent(self, token: str, method: str, path: str, **kwargs):
        response = requests.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kwargs)
        if response.status_code != 200:
            raise SystemExit(f"agent {method} {path} -> {response.status_code}: {response.text}")
        return response.json()


def stamp(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 8) -> str:
    return dt.datetime(2026, month, day, hour, minute, second).strftime("%Y-%m-%dT%H:%M:%S")


def populate(api: Legacy) -> None:
    if api.get("/api/agents"):
        raise SystemExit("already populated: restore a fresh seeded database first")
    offices = {row["code"]: row["id"] for row in api.get("/api/offices")}
    shifts = {row["code"]: row["id"] for row in api.get("/api/shifts")}
    late = api.call("POST", "/api/shifts", json={"code": "LATE", "name": "Late Shift", "start_time": "11:00:00", "end_time": "20:00:00"})
    ho = offices["HO"]

    def employee(code, name, **extra):
        return api.call("POST", "/api/employees", json={"employee_code": code, "full_name": name, "office_id": ho, "joined_on": "2026-01-01", **extra})["id"]

    e1 = employee("E001", "Asha Menon")
    e2 = employee("E002", "Binu Joseph", shift_id=late["id"])
    e3 = employee("E003", "Chitra Nair", shift_id=shifts["NIGHT"])
    e4 = employee("E004", "Deepak Kumar")
    employee("5", "Elena Varghese")  # a code equal to a PIN nobody linked (the eSSL fallback)

    agent = api.call("POST", "/api/agents", json={"code": "OFFICE-001-AGENT", "name": "Head office PC", "office_id": ho})
    token = agent["token"]
    mars = next(row for row in api.get("/api/devices") if row["name"] == "MARS-01")
    api.call("PATCH", f"/api/devices/{mars['id']}", json={"agent_id": agent["agent_id"], "expected_serial": MARS_SERIAL})
    api.call("POST", "/api/devices", json={"name": "MARS-02", "ip_address": "192.168.1.210", "expected_serial": MARS2_SERIAL, "office_id": ho, "agent_id": agent["agent_id"]})
    for serial, address in ((MARS_SERIAL, "192.168.1.209"), (MARS2_SERIAL, "192.168.1.210")):
        api.agent(token, "POST", "/api/agent/devices/announce", json={"serial_number": serial, "ip_address": address, "port": 4370, "model": "x 2008", "platform": "ZAM180_TFT"})
    users = [{"device_user_id": pin, "device_uid": int(pin), "name": f"User {pin}", "privilege": 0, "raw_payload": {"uid": int(pin), "user_id": pin}} for pin in ("1", "2", "3", "4", "5")]
    api.agent(token, "POST", "/api/agent/sync/users", json={"serial_number": MARS_SERIAL, "users": users})
    api.agent(token, "POST", "/api/agent/sync/users", json={"serial_number": MARS2_SERIAL, "users": [{"device_user_id": "4", "device_uid": 4, "name": "Visitor", "privilege": 0}]})
    mars_id = mars["id"]
    for pin, person in (("1", e1), ("2", e2), ("3", e3)):
        api.call("POST", "/api/device-users/map-pin", json={"device_user_id": pin, "employee_id": person, "device_id": mars_id})
    api.call("POST", "/api/device-users/map-pin", json={"device_user_id": "4", "employee_id": e4})  # no device: every terminal (A1)

    api.call("POST", "/api/holidays", json={"holiday_date": "2026-08-15", "name": "Independence Day"})
    api.call("POST", "/api/leave", json={"employee_id": e1, "date_from": "2026-08-11", "date_to": "2026-08-11", "leave_type": "Casual Leave"})
    api.call("POST", "/api/leave", json={"employee_id": e1, "date_from": "2026-08-17", "date_to": "2026-08-17", "leave_type": "Casual Leave", "is_half_day": True})

    uid = iter(range(1000, 2000))
    mars_punches = [
        ("1", stamp(3, 9, 35)),
        ("1", stamp(3, 18, 30)),  # present
        ("1", stamp(4, 9, 45)),
        ("1", stamp(4, 18, 30)),  # late
        ("1", stamp(5, 10, 15)),
        ("1", stamp(5, 18, 30)),  # half day
        ("1", stamp(6, 9, 30)),
        ("1", stamp(6, 9, 30, 20)),  # a double scan (A3)
        ("1", stamp(7, 9, 30)),
        ("1", stamp(7, 10, 31)),  # 61 minutes (A2)
        ("1", stamp(11, 9, 30)),
        ("1", stamp(11, 18, 30)),  # full-day leave with punches (A4)
        ("1", stamp(15, 10, 30)),
        ("1", stamp(15, 19, 30)),  # the holiday worked (A4)
        ("1", stamp(16, 10, 0)),
        ("1", stamp(16, 14, 0)),  # the Sunday worked (A4)
        ("1", stamp(17, 9, 30)),
        ("1", stamp(17, 13, 30)),  # a half-day leave
        ("2", stamp(3, 11, 40)),
        ("2", stamp(3, 20, 0)),  # LATE shift, arrival after start + 30 (A5)
        ("2", stamp(4, 11, 5)),
        ("2", stamp(4, 20, 0)),
        ("3", stamp(3, 22, 0)),
        ("3", stamp(4, 6, 0)),  # night shift
        ("3", stamp(4, 22, 5)),
        ("3", stamp(5, 6, 45)),  # OUT inside the overnight buffer (A6)
        ("4", stamp(3, 9, 30)),
        ("4", stamp(3, 18, 30)),
        ("5", stamp(3, 9, 30)),
        ("5", stamp(3, 18, 30)),  # PIN 5 = employee code 5, never linked
    ]
    records = [{"device_record_uid": next(uid), "device_user_id": pin, "punch_time": at, "status": 15, "punch": 255, "raw_payload": {"capture": True}} for pin, at in mars_punches]
    api.agent(token, "POST", "/api/agent/sync/attendance", json={"serial_number": MARS_SERIAL, "batch_id": "capture-mars-1", "records": records})
    visitor = [
        {"device_record_uid": 1, "device_user_id": "4", "punch_time": stamp(4, 9, 30), "status": 15, "punch": 255},
        {"device_record_uid": 2, "device_user_id": "4", "punch_time": stamp(4, 18, 30), "status": 15, "punch": 255},
    ]
    api.agent(token, "POST", "/api/agent/sync/attendance", json={"serial_number": MARS2_SERIAL, "batch_id": "capture-mars2-1", "records": visitor})

    # the same punch over ADMS (verify → status, status → punch) and then over the agent: eSSL keeps both rows
    api.call("PATCH", f"/api/devices/{mars_id}", json={"adms_enabled": True})
    attlog = "1\t2026-08-18 09:30:00\t255\t15\t0\t0\n"
    pushed = requests.post(f"{api.base}/iclock/cdata", params={"SN": MARS_SERIAL, "table": "ATTLOG", "Stamp": "1"}, data=attlog.encode(), timeout=30)
    if pushed.status_code != 200:
        raise SystemExit(f"ADMS push -> {pushed.status_code}: {pushed.text}")
    api.call("PATCH", f"/api/devices/{mars_id}", json={"adms_enabled": False})
    again = [
        {"device_record_uid": next(uid), "device_user_id": "1", "punch_time": stamp(18, 9, 30), "status": 15, "punch": 255},
        {"device_record_uid": next(uid), "device_user_id": "1", "punch_time": stamp(18, 18, 30), "status": 15, "punch": 255},
    ]
    api.agent(token, "POST", "/api/agent/sync/attendance", json={"serial_number": MARS_SERIAL, "batch_id": "capture-mars-2", "records": again})

    api.call("POST", "/api/attendance/process", json={"date_from": "2026-08-01", "date_to": "2026-08-31"})


def jsonable(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return None
    return value


def masked(table: str, row: dict) -> dict:
    row = {key: jsonable(value) for key, value in row.items() if key not in DROP.get(table, set())}
    field = MASK_NAMES.get(table)
    if field and row.get(field):
        row[field] = f"Person {hashlib.sha256(str(row[field]).encode()).hexdigest()[:6]}"
    return row


def export(dsn: str) -> dict:
    with psycopg.connect(dsn) as connection:
        name = connection.info.dbname
        if name in FORBIDDEN_DATABASES:
            raise SystemExit(f"refusing to read {name}: capture from a private copy only")
        connection.read_only = True
        tables = {}
        with connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
            for table in TABLES:
                cursor.execute(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - fixed table names
                tables[table] = [masked(table, row) for row in cursor.fetchall()]
    return tables


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()
    populate(Legacy(args.base))
    tables = export(args.dsn)
    (HERE / "essl_attendance_tables.json").write_text(json.dumps(tables, indent=1, sort_keys=True) + "\n")
    print({table: len(rows) for table, rows in tables.items()})


if __name__ == "__main__":
    main()
