"""Build the eSSL legacy fixtures used by ``hr/tests/test_legacy_*.py`` (run by hand, never by pytest).

The fixtures are produced by the eSSL application itself — never hand-written rows:

1. a **private** Postgres database is migrated with the eSSL app's own alembic chain and seeded with its own
   ``scripts/seed.py`` (roles ADMIN/HR/USER/VIEWER, ``admin``, offices HO/BR1/BR2, shifts GEN/NIGHT, one device);
2. a private eSSL API server (uvicorn on a free port 18100–18199) is driven through its **own endpoints** to create
   representative offices, shifts, employees (with logins of every role, a revoked login, an inactive employee),
   holidays (office, global, deactivated, and the duplicate global date the legacy PATCH lets through) and leave
   records (every status, free-text leave types differing only in case, a multi-day half day);
3. ``attendance_rules`` has no API in eSSL, so its rows are inserted with SQL (the only table written directly);
4. every table is exported read-only (``SELECT *``) to ``essl_tables.json`` and the legacy API's GET responses to
   ``essl_api.json``, both **masked** (e-mail local parts, phone digits and people's names are replaced
   deterministically; the synthetic passwords below are test values, their bcrypt hashes are kept so the import can
   prove old passwords keep working).

Usage (see docs/decisions/hr.md "Parity evidence")::

    createdb essl_wp_hr
    cd <copy of essl-webap-main/backend>
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/essl_wp_hr /home/user/.venvs/essl/bin/alembic upgrade head
    DATABASE_URL=... /home/user/.venvs/essl/bin/python -m scripts.seed
    DATABASE_URL=... /home/user/.venvs/essl/bin/uvicorn app.main:app --port 18151 &
    /home/user/.venvs/platform/bin/python hr/tests/legacy/capture_essl.py --base http://127.0.0.1:18151 --dsn postgresql://postgres:postgres@localhost:5432/essl_wp_hr
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import re
from pathlib import Path

import psycopg
import requests

HERE = Path(__file__).resolve().parent
FORBIDDEN_DATABASES = {"legacy_goldenapp", "legacy_blog_cms", "essl_attendance"}
TABLES = ["roles", "users", "offices", "shifts", "employees", "holidays", "leave_records", "attendance_rules"]

# Synthetic test passwords (never real ones). The import tests log in with them.
PASSWORDS = {"asha": "asha-legacy-pass-1", "binu": "binu-legacy-pass-2", "chitra": "chitra-legacy-pass-3", "elena": "elena-legacy-pass-4", "farhan": "farhan-legacy-pass-5", "gita": "g" * 80}


class Legacy:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        token = self.session.post(f"{self.base}/api/auth/login", json={"username": "admin", "password": "admin123"}, timeout=10).json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, **kwargs):
        response = self.session.request(method, f"{self.base}{path}", timeout=30, **kwargs)
        if response.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json()

    def get(self, path: str, **params):
        return self.call("GET", path, params=params)


def populate(api: Legacy) -> None:
    offices = {row["code"]: row for row in api.get("/api/offices")}
    shifts = {row["code"]: row for row in api.get("/api/shifts")}
    if "SALES" in offices:
        raise SystemExit("already populated: restore a fresh seeded database first")

    # --- shifts -----------------------------------------------------------------------------------------------
    early = api.call(
        "POST",
        "/api/shifts",
        json={
            "code": "EARLY",
            "name": "Early Shift",
            "start_time": "07:00",
            "end_time": "16:00",
            "grace_minutes": 5,
            "late_threshold_minutes": 10,
            "early_exit_threshold_minutes": 10,
            "full_day_minutes": 450,
            "half_day_minutes": 225,
            "break_minutes": 45,
            "overtime_enabled": False,
            "overtime_after_minutes": 540,
            "working_days": [0, 1, 2, 3, 4],
            "weekly_off_days": [5, 6],
            "auto_deduct_break": False,
        },
    )
    half = api.call(
        "POST",
        "/api/shifts",
        json={
            "code": "HALFSAT",
            "name": "Saturday Half Day",
            "office_id": offices["HO"]["id"],
            "start_time": "09:30",
            "end_time": "13:30",
            "break_minutes": 0,
            "full_day_minutes": 240,
            "half_day_minutes": 120,
            "working_days": [5],
            "weekly_off_days": [6],
        },
    )
    # The legacy API accepts an end before the start on a shift not marked overnight (the engine then adds a day).
    api.call("POST", "/api/shifts", json={"code": "LATE", "name": "Late Shift", "start_time": "20:00", "end_time": "04:00", "is_overnight": False})
    old = api.call("POST", "/api/shifts", json={"code": "OLD", "name": "Retired Shift", "start_time": "10:00", "end_time": "19:00"})
    api.call("PATCH", f"/api/shifts/{old['id']}", json={"is_active": False})

    # --- offices ----------------------------------------------------------------------------------------------
    sales = api.call("POST", "/api/offices", json={"code": "SALES", "name": "Sales Office", "address": "2nd Floor, MG Road, Kochi", "timezone": "Asia/Kolkata", "default_shift_id": early["id"]})
    dubai = api.call("POST", "/api/offices", json={"code": "DXB", "name": "Dubai Liaison", "timezone": "Asia/Dubai"})
    api.call("POST", "/api/offices", json={"code": "LAB", "name": "Test Lab", "timezone": "Asia/Calcutta"})
    api.call("POST", "/api/offices", json={"code": "WFH", "name": "Remote", "timezone": "India Standard Time"})  # not validated by eSSL
    api.call("POST", f"/api/offices/{offices['BR2']['id']}/deactivate")

    ho, br1 = offices["HO"]["id"], offices["BR1"]["id"]
    gen, night = shifts["GEN"]["id"], shifts["NIGHT"]["id"]

    # --- employees (+ logins through the employee endpoints, the only way eSSL creates users) -------------------
    def employee(**body):
        return api.call("POST", "/api/employees", json=body)

    employee(
        employee_code="E001",
        full_name="Asha Menon",
        office_id=ho,
        shift_id=gen,
        department="Accounts",
        designation="Accountant",
        email="asha.menon@example.com",
        phone="+91 98470 12345",
        joined_on="2023-04-03",
        attendance_identity_method="FACE",
        login_username="asha",
        login_password=PASSWORDS["asha"],
        login_role="USER",
    )
    employee(
        employee_code="E002",
        full_name="Binu Joseph",
        office_id=ho,
        department="HR",
        designation="HR Executive",
        email="binu.joseph@example.com",
        phone="9847012346",
        joined_on="2022-11-14",
        login_username="binu",
        login_password=PASSWORDS["binu"],
        login_role="HR",
    )
    employee(
        employee_code="E003",
        full_name="Chitra Nair",
        office_id=sales["id"],
        shift_id=night,
        department="Sales",
        designation="Sales Executive",
        email="chitra.nair@example.com",
        phone="0484 2345678",
        attendance_identity_method="CARD",
        login_username="chitra",
        login_password=PASSWORDS["chitra"],
        login_role="VIEWER",
    )
    employee(employee_code="E004", full_name="Deepak Kumar", office_id=br1, shift_id=early["id"], department="Projects", designation="Installer", phone="12345")
    employee(
        employee_code="E005",
        full_name="Elena Varghese",
        office_id=dubai["id"],
        designation="Director",
        phone="+971 50 123 4567",
        login_username="elena",
        login_password=PASSWORDS["elena"],
        login_role="ADMIN",
    )  # no e-mail on the employee → login without e-mail
    farhan = employee(
        employee_code="E006", full_name="Farhan Ali", office_id=ho, shift_id=half["id"], email="farhan.ali@example.com", login_username="farhan", login_password=PASSWORDS["farhan"], login_role="USER"
    )
    api.call("POST", f"/api/employees/{farhan['id']}/revoke-login")
    api.call("POST", f"/api/employees/{farhan['id']}/deactivate")
    gita = employee(
        employee_code="e007",
        full_name="Gita Pillai",
        office_id=br1,
        attendance_identity_method="FACE_CARD",
        joined_on="2024-01-15",
        email="gita.pillai@example.com",
        login_username="gita",
        login_password=PASSWORDS["gita"],
        login_role="USER",
    )
    api.call("POST", f"/api/employees/{gita['id']}/deactivate")  # eSSL keeps the login active
    employee(employee_code="E008", full_name="Hari Das", attendance_identity_method="CARD", email="not-an-email")
    employee(employee_code="E009", full_name="Indu Mohan", office_id=sales["id"], email="indu.mohan@example.com", phone="+91 99950 00009", joined_on="2025-06-01")

    # --- holidays ---------------------------------------------------------------------------------------------
    api.call("POST", "/api/holidays", json={"holiday_date": "2026-01-26", "name": "Republic Day"})
    api.call("POST", "/api/holidays", json={"holiday_date": "2026-08-26", "name": "Thiruvonam", "notes": "State holiday"})
    api.call("POST", "/api/holidays", json={"holiday_date": "2026-03-10", "name": "Foundation Day", "office_id": ho})
    api.call("POST", "/api/holidays", json={"holiday_date": "2026-03-10", "name": "Sales Offsite", "office_id": sales["id"]})
    retired = api.call("POST", "/api/holidays", json={"holiday_date": "2026-05-01", "name": "May Day"})
    api.call("POST", f"/api/holidays/{retired['id']}/deactivate")
    duplicate = api.call("POST", "/api/holidays", json={"holiday_date": "2026-10-02", "name": "Gandhi Jayanti (typo)"})
    api.call("PATCH", f"/api/holidays/{duplicate['id']}", json={"holiday_date": "2026-01-26"})  # PATCH has no duplicate check

    # --- leave ------------------------------------------------------------------------------------------------
    employees = {row["employee_code"]: row["id"] for row in api.get("/api/employees", presence="ALL", include_inactive="true", page_size=100)["items"]}

    def leave(code, **body):
        return api.call("POST", "/api/leave", json={"employee_id": employees[code], **body})

    leave("E001", date_from="2026-02-02", date_to="2026-02-04", leave_type="LEAVE", reason="Family function")
    leave("E001", date_from="2026-03-05", date_to="2026-03-05", leave_type="Sick", is_half_day=True)
    leave("E003", date_from="2026-04-10", date_to="2026-04-11", leave_type="Casual Leave", status="PENDING", reason="Travel")
    leave("E004", date_from="2026-04-20", date_to="2026-04-20", leave_type="LEAVE", status="REJECTED")
    leave("E002", date_from="2026-05-04", date_to="2026-05-05", leave_type="sick", status="CANCELLED")
    leave("e007", date_from="2026-01-12", date_to="2026-01-13", leave_type="Sick", is_half_day=True)  # a two-day "half day"
    leave("E005", date_from="2026-06-15", date_to="2026-06-19", leave_type="On Duty", reason="Client visit, Muscat")
    leave("E001", date_from="2026-02-04", date_to="2026-02-05", leave_type="LEAVE")  # overlaps the first one
    print("populated through the legacy API")


RULES = [
    ("Company half-day deadline", None, None, {"half_day_after": "10:30"}, None, True, "Global default"),
    ("Head office allowance", "HO", None, {"half_day_after_minutes": 45, "grace_bonus": 5}, "2025-01-01", True, None),
    ("Night shift short day", None, "NIGHT", {"half_day_under_minutes": 240}, None, True, None),
    ("Sales early shift", "SALES", "EARLY", {"half_day_after": "08:15:00"}, "2026-01-01", True, "Office and shift both named"),
    ("Broken deadline", None, "GEN", {"half_day_after": "25:99", "half_day_under_minutes": -5}, None, True, None),
    ("Future policy", None, None, {"half_day_after_minutes": 60}, "2027-04-01", False, "Not yet active"),
]


def insert_rules(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for name, office, shift, rules, effective_from, active, notes in RULES:
            cur.execute(
                "INSERT INTO attendance_rules (name, office_id, shift_id, rules, effective_from, is_active, notes, created_at, updated_at) "
                "VALUES (%s, (SELECT id FROM offices WHERE code = %s), (SELECT id FROM shifts WHERE code = %s), %s, %s, %s, %s, now(), now())",
                (name, office, shift, json.dumps(rules), effective_from, active, notes),
            )
    print("inserted attendance_rules (eSSL has no API for them)")


# --- masking ------------------------------------------------------------------------------------------------------
def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mask_phone(value):
    """Keep the shape (country code, spacing, length) and the first four characters; replace the other digits."""
    if not value:
        return value
    digest = _digest(value)
    out, seen = [], 0
    for char in value:
        if char.isdigit():
            seen += 1
            out.append(char if seen <= 4 else str(int(digest[seen], 16) % 10))
        else:
            out.append(char)
    return "".join(out)


def mask_email(value):
    if not value or "@" not in value:
        return value
    return f"person-{_digest(value.lower())[:8]}@example.test"


def mask_name(value):
    """Deterministic (the same person reads the same in both exports)."""
    if not value:
        return value
    return f"Person {_digest(value)[:6].upper()}"


def masked_tables(dsn: str) -> dict:
    tables = {}
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for table in TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")  # read-only export
            tables[table] = [dict(row) for row in cur.fetchall()]
    for row in tables["users"]:
        row["email"] = mask_email(row["email"])
        row["full_name"] = mask_name(row["full_name"])
    for row in tables["employees"]:
        row["email"] = mask_email(row["email"])
        row["phone"] = mask_phone(row["phone"])
        row["full_name"] = mask_name(row["full_name"])
    for row in tables["leave_records"]:
        if row["reason"]:
            row["reason"] = re.sub(r"[A-Za-z]{4,}", lambda m: "x" * len(m.group()), row["reason"])
    return tables


def masked_api(api: Legacy) -> dict:
    data = {
        "offices": api.get("/api/offices"),
        "shifts": api.get("/api/shifts"),
        "employees": api.get("/api/employees", presence="ALL", include_inactive="true", page_size=100)["items"],
        "holidays": api.get("/api/holidays"),
        "leave": api.get("/api/leave"),
    }
    for row in data["employees"]:
        row["email"] = mask_email(row["email"])
        row["phone"] = mask_phone(row["phone"])
        row["full_name"] = mask_name(row["full_name"])
    for row in data["leave"]:
        row["employee_name"] = mask_name(row["employee_name"])
        if row["reason"]:
            row["reason"] = re.sub(r"[A-Za-z]{4,}", lambda m: "x" * len(m.group()), row["reason"])
    return data


def _default(value):
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="private eSSL API, e.g. http://127.0.0.1:18151")
    parser.add_argument("--dsn", required=True, help="the private eSSL database")
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    database = args.dsn.rsplit("/", 1)[-1].split("?")[0]
    if database in FORBIDDEN_DATABASES:
        raise SystemExit(f"refusing to write to {database}: use a private copy")
    api = Legacy(args.base)
    if not args.export_only:
        populate(api)
        insert_rules(args.dsn)
    (HERE / "essl_tables.json").write_text(json.dumps(masked_tables(args.dsn), indent=1, default=_default, sort_keys=True) + "\n")
    (HERE / "essl_api.json").write_text(json.dumps(masked_api(api), indent=1, default=_default, sort_keys=True) + "\n")
    print("wrote essl_tables.json and essl_api.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
