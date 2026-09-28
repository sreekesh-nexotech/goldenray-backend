"""Parity evidence: every field the eSSL API returned (captured from the real app in essl_api.json) is served by the new
hr endpoints after the import — renamed where the platform names differ, repaired only where the import reports it.

Field map (eSSL → platform): ``employee_code`` → ``code``; ``office_name``/``shift_name``/``effective_shift_name`` →
``office.name``/``shift.name``/``effective_shift.name``; ``attendance_identity_method`` → ``identity_method``;
``phone`` → ``phone_e164`` (E.164); ``login_role`` → ``user.role`` (ADMIN→admin, HR→hr, USER/VIEWER→staff);
``holiday_date`` → ``date``; ``leave_type`` (text) → ``leave_type.name``; integer ids → uids. Not carried:
``device_presence``, ``active_on_devices``, ``device_user_ids``, ``device_count`` (devices package), ``office_id`` of a
shift (informational in eSSL).
"""

import json
from pathlib import Path

import pytest

from hr.services import legacy_import
from hr.services.validation import phone_e164

pytestmark = pytest.mark.django_db
LEGACY = Path(__file__).resolve().parent / "legacy"
ROLE = {"ADMIN": "admin", "HR": "hr", "USER": "staff", "VIEWER": "staff"}
SHIFT_FIELDS = [
    "name",
    "start_time",
    "end_time",
    "grace_minutes",
    "late_threshold_minutes",
    "early_exit_threshold_minutes",
    "full_day_minutes",
    "half_day_minutes",
    "break_minutes",
    "overtime_enabled",
    "overtime_after_minutes",
    "working_days",
    "weekly_off_days",
    "auto_deduct_break",
    "is_active",
    "employee_count",
    "half_day_after",
    "half_day_after_source",
]


@pytest.fixture
def legacy_api():
    return json.loads((LEGACY / "essl_api.json").read_text())


@pytest.fixture
def client(hr_client):
    legacy_import.import_all(json.loads((LEGACY / "essl_tables.json").read_text()))
    return hr_client


def rows(client, path, **params):
    return client.get(f"/api/v1/hr/{path}/", {"page_size": 200, **params}).json()["results"]


def name(ref):
    return ref["name"] if ref else None


def test_offices(client, legacy_api):
    new = {row["code"]: row for row in rows(client, "offices")}
    for old in legacy_api["offices"]:
        row = new[old["code"]]
        assert (row["name"], row["address"] or None, row["is_active"], row["employee_count"]) == (old["name"], old["address"], old["is_active"], old["employee_count"]), old["code"]
        assert row["timezone"] == (old["timezone"] if old["code"] != "WFH" else "Asia/Kolkata")  # "India Standard Time" is no zone
        assert name(row["default_shift"]) == old["default_shift_name"]
        settings = old["attendance_settings"]
        if settings is None:
            assert row["attendance_settings"] is None
        else:
            assert row["attendance_settings"]["shift"]["code"] == settings["shift_code"]
            assert row["attendance_settings"]["shift"]["start_time"][:5] == settings["attendance_start_time"]
            for key in ("grace_minutes", "late_threshold_minutes", "half_day_minutes", "full_day_minutes", "break_minutes", "overtime_after_minutes", "working_days", "weekly_off_days"):
                assert row["attendance_settings"][key] == settings[key], (old["code"], key)


def test_shifts(client, legacy_api):
    new = {row["code"]: row for row in rows(client, "shifts")}
    for old in legacy_api["shifts"]:
        row = new[old["code"]]
        assert {field: row[field] for field in SHIFT_FIELDS} == {field: old[field] for field in SHIFT_FIELDS}, old["code"]
        # eSSL stored LATE (20:00–04:00) as a day shift and silently added a day; the import marks it overnight (reported)
        assert row["is_overnight"] == (old["is_overnight"] or old["code"] == "LATE")


def test_employees(client, legacy_api):
    new = {row["code"]: row for row in rows(client, "employees", include_inactive="true")}
    assert len(new) == len(legacy_api["employees"])
    for old in legacy_api["employees"]:
        row = new[old["employee_code"]]
        assert (row["full_name"], row["department"] or None, row["designation"] or None, row["joined_on"], row["is_active"]) == (
            old["full_name"],
            old["department"],
            old["designation"],
            old["joined_on"],
            old["is_active"],
        ), old["employee_code"]
        assert (name(row["office"]), name(row["shift"]), name(row["effective_shift"])) == (old["office_name"], old["shift_name"], old["effective_shift_name"])
        assert row["identity_method"] == old["attendance_identity_method"]
        assert row["email"] == ("" if old["email"] == "not-an-email" else old["email"] or "")
        expected_phone = ""
        if old["phone"]:
            try:
                expected_phone = phone_e164(old["phone"])
            except Exception:  # noqa: BLE001 - the import reports unparsable numbers and leaves them empty
                expected_phone = ""
        assert row["phone_e164"] == expected_phone
        if old["user_id"] is None:
            assert row["user"] is None
        else:
            assert row["user"]["role"]["slug"] == ROLE[old["login_role"]]


def test_holidays(client, legacy_api):
    new = {(row["date"], row["name"]): row for row in rows(client, "holidays")}
    for old in legacy_api["holidays"]:
        key = (old["holiday_date"], old["name"])
        if old["name"] == "Gandhi Jayanti (typo)":  # the second global holiday on 2026-01-26 eSSL's PATCH allowed
            assert key not in new
            continue
        row = new[key]
        assert (name(row["office"]), row["is_active"], row["notes"] or None) == (old["office_name"], old["is_active"], old["notes"])


def test_leave(client, legacy_api):
    new = {(row["employee"]["code"], row["date_from"], row["date_to"]): row for row in rows(client, "leave")}
    assert len(new) == len(legacy_api["leave"])
    for old in legacy_api["leave"]:
        row = new[(old["employee_code"], old["date_from"], old["date_to"])]
        assert (row["status"], row["reason"] or None) == (old["status"], old["reason"])
        assert row["leave_type"]["name"].lower() == old["leave_type"].lower()  # "sick" shares the "Sick" type
        # a half day over two dates (eSSL accepted it) is imported as full days (reported)
        assert row["is_half_day"] == (old["is_half_day"] and old["date_from"] == old["date_to"])
