"""Parity with the eSSL API (devices/tests/legacy/essl_devices_api.json) after importing the same eSSL tables.

The responses were captured from a private eSSL instance right after the scenario that produced the tables. What a
terminal user, a device and an agent ARE, and every state derived from the stored evidence (device_state,
software_state, the reconciliation categories and counters, the presence of each employee) must come out the same.
Intended differences (docs/decisions/devices.md): ADMS push is imported off (PROJECT-01 is UNASSIGNED until a token is
issued), and time-derived health is judged now, not at capture time.
"""

import json
from pathlib import Path

import pytest

from devices.services import legacy_import
from hr.services import legacy_import as hr_import

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "legacy"
LEGACY = json.loads((FIXTURES / "essl_devices_api.json").read_text())
TABLES = json.loads((FIXTURES / "essl_devices_tables.json").read_text())
CODES = {row["id"]: row["employee_code"] for row in TABLES["employees"]}


@pytest.fixture
def api(admin_client):
    hr_import.import_all(TABLES)
    legacy_import.import_all(TABLES)
    return admin_client


def blank(value):
    return "" if value is None else value


def ours(api, path, **params):
    body = api.get(path, {"page_size": 200, **params}).json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def test_devices(api):
    rows = {row["name"]: row for row in ours(api, "/api/v1/devices/")}
    assert set(rows) == {row["name"] for row in LEGACY["devices"]}
    for legacy in LEGACY["devices"]:
        mine = rows[legacy["name"]]
        want = (
            legacy["serial_number"],
            legacy["expected_serial"],
            legacy["ip_address"],
            legacy["port"],
            blank(legacy["mac_address"]),
            blank(legacy["expected_mac"]),
            legacy["office_name"],
            legacy["agent_code"],
            legacy["identity_status"],
            legacy["is_active"],
            blank(legacy["model"]),
            blank(legacy["firmware_version"]),
            blank(legacy["platform"]),
            legacy["user_count"],
            legacy["attendance_count"],
            legacy["awaiting_discovery"],
            legacy["mapping_consistent"],
        )
        got = (
            mine["serial_number"],
            mine["expected_serial"],
            mine["ip_address"],
            mine["port"],
            mine["mac_address"],
            mine["expected_mac"],
            mine["office"]["name"] if mine["office"] else None,
            mine["agent"]["code"] if mine["agent"] else None,
            mine["identity_status"],
            mine["is_active"],
            mine["model"],
            mine["firmware_version"],
            mine["platform"],
            mine["user_count"],
            mine["attendance_count"],
            mine["health"]["awaiting_discovery"],
            mine["health"]["mapping_consistent"],
        )
        assert got == want, legacy["name"]
        expected_transport = "UNASSIGNED" if legacy["transport"] == "ADMS_PUSH" else legacy["transport"]  # ADMS imported off
        assert mine["health"]["transport"] == expected_transport, legacy["name"]


def test_agents(api):
    rows = {row["code"]: row for row in ours(api, "/api/v1/devices/agents/")}
    for legacy in LEGACY["agents"]:
        mine = rows[legacy["code"]]
        assert (mine["name"], mine["office"]["name"], sorted(device["name"] for device in mine["devices"]), mine["status"]) == (
            legacy["name"],
            legacy["office_name"],
            sorted(device["device_name"] for device in legacy["devices"]),
            legacy["status"],
        ), legacy["code"]


def test_device_users(api):
    rows = {(row["device"]["name"], row["pin"]): row for row in ours(api, "/api/v1/devices/device-users/")}
    assert set(rows) == {(row["device_name"], row["device_user_id"]) for row in LEGACY["device_users"]}
    for legacy in LEGACY["device_users"]:
        mine = rows[(legacy["device_name"], legacy["device_user_id"])]
        keys = ("device_state", "software_state", "is_active_user", "needs_device_removal", "sync_state", "device_active", "has_password", "privilege")
        assert {key: mine[key] for key in keys} == {key: legacy[key] for key in keys}, legacy["device_user_id"]
        assert mine["name"] == blank(legacy["name"])
        assert (mine["employee"]["code"] if mine["employee"] else None) == CODES.get(legacy["employee_id"])
        assert (mine["card"], mine["group_id"], mine["device_uid"]) == (blank(legacy["card"]), blank(legacy["group_id"]), legacy["device_uid"])


def _device_uid(api, name):
    return next(row["uid"] for row in ours(api, "/api/v1/devices/") if row["name"] == name)


@pytest.mark.parametrize("name", ["MARS-01", "SALES-01"])
def test_user_reconciliation(api, name):
    mine = api.get(f"/api/v1/devices/{_device_uid(api, name)}/user-reconciliation/").json()
    legacy = LEGACY["user_reconciliation"][name]
    keys = (
        "total_mappings",
        "active_on_device",
        "missing_from_device",
        "pending_sync",
        "sync_failed",
        "device_inactive",
        "deactivated_in_software",
        "unlinked",
        "sync_state",
        "device_is_active",
        "device_deletion_supported",
    )
    assert {key: mine[key] for key in keys} == {key: legacy[key] for key in keys}
    assert [item["pin"] for item in mine["pending_device_removals"]] == [item["device_user_id"] for item in legacy["pending_device_removals"]]


@pytest.mark.parametrize("name", ["MARS-01", "SALES-01"])
def test_employee_reconciliation(api, name):
    mine = api.get(f"/api/v1/devices/{_device_uid(api, name)}/employee-reconciliation/").json()
    legacy = LEGACY["employee_reconciliation"][name]
    assert mine["summary"] == legacy["summary"] and mine["transport"] == legacy["transport"]
    rows = {row["pin"]: row for row in mine["rows"]}
    for row in legacy["rows"]:
        got = rows[row["device_user_id"]]
        keys = ("category", "available_action", "requires_confirmation", "device_state", "software_state", "is_active_user", "ambiguous")
        assert {key: got[key] for key in keys} == {key: row[key] for key in keys}, row["device_user_id"]
        assert (got["employee"]["code"] if got["employee"] else None) == row["employee_code"]
        assert (got["suggested_employee"]["code"] if got["suggested_employee"] else None) == row["suggested_employee_code"]
        assert sorted(candidate["code"] for candidate in got["candidate_employees"]) == sorted(candidate.get("employee_code") or candidate.get("code") for candidate in row["candidate_employees"])


@pytest.mark.parametrize("code", sorted(LEGACY["device_mappings"]))
def test_device_mappings(api, code):
    from hr.models import Employee

    employee = Employee.objects.get(code=code)
    mine = api.get(f"/api/v1/hr/employees/{employee.uid}/device-mappings/").json()
    legacy = LEGACY["device_mappings"][code]
    got = sorted((row["device"]["name"], row["pin"], row["device_state"], row["software_state"], row["is_active_user"], row["needs_device_removal"]) for row in mine["mappings"])
    want = sorted((row["device_name"], row["device_user_id"], row["device_state"], row["software_state"], row["is_active_user"], row["needs_device_removal"]) for row in legacy["mappings"])
    assert got == want
    assert (mine["details"]["active_on_devices"], mine["details"]["missing_from_devices"], mine["details"]["device_deletion_supported"]) == (
        legacy["active_on_devices"],
        legacy["missing_from_devices"],
        legacy["device_deletion_supported"],
    )


def test_protocol_mappings(api):
    got = {
        (row["field"], row["raw_value"], row["meaning_type"], row["meaning_code"], row["label"], row["confidence"], row["device_platform"], row["firmware_version"])
        for row in ours(api, "/api/v1/devices/protocol-mappings/")
    }
    want = {
        (row["field"], row["raw_value"], row["meaning_type"], row["meaning_code"], blank(row["label"]), row["confidence"], blank(row["device_platform"]), blank(row["firmware_version"]))
        for row in LEGACY["protocol_mappings"]
    }
    assert got == want


def test_mapping_report(api):
    mine = api.get("/api/v1/devices/mapping/").json()
    assert {office["office"]["name"]: (sorted(office["devices"]), sorted(office["agents"])) for office in mine["offices"]} == {
        office["office_name"]: (sorted(office["devices"]), sorted(office["agents"])) for office in LEGACY["mapping"]["offices"]
    }
    assert {agent["agent"]["code"]: sorted(agent["serves_devices"]) for agent in mine["agents"]} == {agent["agent_code"]: sorted(agent["serves_devices"]) for agent in LEGACY["mapping"]["agents"]}
    assert [device["name"] for device in mine["inconsistencies"]] == [device["device_name"] for device in LEGACY["mapping"]["inconsistencies"]]
