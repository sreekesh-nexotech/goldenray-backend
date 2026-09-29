"""The eSSL devices import (PLAN §7.5) on the masked tables captured from a private eSSL instance.

hr imports first (offices, employees), then devices: agents (new credentials), devices, device users (per-device links,
multi-device PINs reported), the presence watermarks and the protocol mappings. Idempotent through core_legacy_map.
"""

import copy
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from audit.models import AuditLog
from core.models import LegacyMap
from devices.models import AdmsRequest, AdmsUnknownDevice, Agent, Device, DeviceUser, ProtocolMapping, SyncLog
from devices.services import health, legacy_import, roster
from hr.services import legacy_import as hr_import

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "legacy"


@pytest.fixture
def tables():
    return json.loads((FIXTURES / "essl_devices_tables.json").read_text())


CAPTURED = datetime(2026, 9, 30, tzinfo=dt_timezone.utc)  # the day after the capture: its ADMS evidence is recent


@pytest.fixture
def imported(tables):
    hr_import.import_all(tables)
    return legacy_import.import_all(tables, now=CAPTURED)


def violations(report, field=None):
    return [entry for entry in report["violations"] if field is None or entry["field"] == field]


class TestImport:
    def test_every_table(self, imported, tables):
        assert {name: (report["created"], report["updated"]) for name, report in imported.items()} == {
            "agents": (3, 0),
            "devices": (5, 0),
            "device_users": (8, 0),
            "sync_logs": (2, 0),
            "protocol_mappings": (3, 0),
            "adms_unknown_devices": (1, 0),
            "adms_requests": (11, 0),
        }
        assert imported["sync_logs"]["skipped"] == 6  # older USERS logs and other log types are not migrated
        assert Device.objects.count() == 5 and DeviceUser.objects.count() == 8 and ProtocolMapping.objects.count() == 3
        assert (
            LegacyMap.objects.filter(source_system="ESSL", source_table__in=["agents", "devices", "device_users", "sync_logs", "protocol_mappings", "adms_unknown_devices", "adms_requests"]).count()
            == 33
        )
        assert AuditLog.objects.filter(action="devices.legacy_imported").count() == 7

    def test_rerun_changes_nothing(self, imported, tables):
        again = legacy_import.import_all(tables, now=CAPTURED)
        assert all(report["created"] == 0 and report["updated"] == 0 for report in again.values())
        assert again["adms_requests"]["skipped"] == 11 and AdmsRequest.objects.count() == 11
        assert again["agents"]["credentials"] == [] and SyncLog.objects.count() == 2

    def test_rerun_updates_changed_rows_and_keeps_deleted_ones_deleted(self, imported, tables):
        changed = copy.deepcopy(tables)
        changed["devices"][0]["notes"] = "moved to the lobby"
        Device.objects.get(name="OLD-TERMINAL").soft_delete(None)
        again = legacy_import.import_devices(changed["devices"])
        assert (again["created"], again["updated"], again["skipped"]) == (0, 1, 4)  # 3 unchanged + the deleted one
        assert Device.objects.get(name="MARS-01").notes == "moved to the lobby" and not Device.objects.filter(name="OLD-TERMINAL").exists()


class TestAgents:
    def test_new_credentials_are_issued_once_and_work(self, imported):
        credentials = imported["agents"]["credentials"]
        assert [entry["agent_code"] for entry in credentials] == ["OFFICE-001-AGENT", "OFFICE-002-AGENT"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials[0]['token']}")
        config = client.get("/api/agent/v1/config/").json()
        assert config["agent"]["code"] == "OFFICE-001-AGENT" and {device["name"] for device in config["devices"]} == {"MARS-01", "MARS-02"}

    def test_revoked_agent_is_disabled_without_credential(self, imported):
        revoked = Agent.objects.select_related("credential").get(code="OFFICE-003-AGENT")
        assert revoked.is_active is False and revoked.credential is None and health.agent_status(revoked) == health.REVOKED
        assert any("revoked in eSSL" in entry["message"] for entry in violations(imported["agents"], "token_revoked_at"))

    def test_bcrypt_hashes_are_not_carried(self, imported):
        assert all(agent.settings.get("token_hash") is None for agent in Agent.objects.all())


class TestDevices:
    def test_values_and_repairs(self, imported):
        mars = Device.objects.select_related("office", "agent").get(name="MARS-01")
        assert (mars.serial_number, mars.mac_address, mars.agent.code, mars.office.name, mars.identity_status) == ("NCD8253601138", "00:17:61:12:9c:49", "OFFICE-001-AGENT", "Main Office", "VERIFIED")
        assert mars.identity_checked_at is not None and mars.last_punch_at is not None and mars.attendance_count == 3
        project = Device.objects.get(name="PROJECT-01")
        assert project.adms_enabled is False and project.adms_token_hash is None and health.transport(project) == health.UNASSIGNED
        assert [entry["source_id"] for entry in violations(imported["devices"], "adms_enabled")] == ["3"]
        old = Device.all_objects.get(name="OLD-TERMINAL")
        assert old.is_active is False and old.protocol == "ZK_UDP"
        assert Device.objects.get(name="MARS-02").identity_status == "IDENTITY_MISMATCH"

    def test_unusable_rows(self, imported, tables):
        rows = [
            {"id": 90, "name": "", "ip_address": "10.0.0.1"},
            {"id": 91, "name": "NOWHERE"},
            {"id": 92, "name": "ODD", "ip_address": "not-an-ip", "expected_serial": " NCD9 ", "mac_address": "zz", "port": 0, "protocol": "ADMS_PUSH", "identity_status": "WHO", "office_id": 999},
        ]
        report = legacy_import.import_devices(rows)
        assert report["created"] == 1 and {entry["source_id"] for entry in report["violations"]} == {"90", "91", "92"}
        odd = Device.objects.get(name="ODD")
        assert (odd.ip_address, odd.expected_serial, odd.mac_address, odd.port, odd.protocol, odd.identity_status, odd.office_id) == (None, "NCD9", "", 4370, "ZK_TCP", "UNVERIFIED", None)


class TestDeviceUsers:
    def test_links_stay_per_device_and_multi_device_pins_are_reported(self, imported):
        links = {(row.device.name, row.pin): row.employee.code if row.employee else None for row in DeviceUser.objects.select_related("device", "employee")}
        assert links == {
            ("MARS-01", "1"): "E001",
            ("MARS-01", "2"): "E002",
            ("MARS-01", "3"): "3",
            ("MARS-01", "EMP004"): None,
            ("MARS-01", "9"): None,
            ("SALES-01", "1"): "E003",
            ("SALES-01", "2"): "E002",
            ("SALES-01", "5"): None,
        }
        reported = {entry["source_id"]: entry["message"] for entry in violations(imported["device_users"], "pin")}
        assert set(reported) == {"1", "2"} and "MARS-01 → E001" in reported["1"] and "SALES-01 → E003" in reported["1"]

    def test_the_watermark_survives(self, imported):
        sales = Device.objects.get(name="SALES-01")
        mark = roster.watermarks([sales.pk])[sales.pk]
        assert mark.present == frozenset({"1", "2"}) and mark.sync_state == roster.SYNC_OK
        described = roster.describe(DeviceUser.objects.filter(device=sales).select_related("device", "employee"))
        by_pin = {row.pin: described[row.pk]["device_state"] for row in DeviceUser.objects.filter(device=sales)}
        assert by_pin == {"1": "ACTIVE_ON_DEVICE", "2": "ACTIVE_ON_DEVICE", "5": "MISSING_FROM_DEVICE"}

    def test_terminal_user_passwords_are_not_imported(self, imported, admin_client):
        """eSSL stored pyzk's whole user object (``vars(user)``) as raw_payload: the terminal user's password in clear."""
        raw = {"uid": 12, "user_id": "12", "name": "Ravi", "privilege": 0, "password": "4321", "group_id": "1", "card": 0}
        report = legacy_import.import_device_users([{"id": 93, "device_id": 1, "device_user_id": "12", "name": "Ravi", "has_password": True, "raw_payload": raw}])
        assert report["created"] == 1
        row = DeviceUser.objects.get(device__name="MARS-01", pin="12")
        assert row.raw_payload == {**raw, "password": "***"} and row.has_password is True
        listed = admin_client.get("/api/v1/devices/device-users/", {"search": "Ravi"}).json()["results"]
        assert [item["raw_payload"]["password"] for item in listed] == ["***"]

    def test_unresolvable_rows(self, imported):
        report = legacy_import.import_device_users(
            [{"id": 90, "device_id": 999, "device_user_id": "1"}, {"id": 91, "device_id": 1, "device_user_id": ""}, {"id": 92, "device_id": 1, "device_user_id": "77", "employee_id": 999}]
        )
        assert report["created"] == 1 and {entry["field"] for entry in report["violations"] if entry["source_id"] in ("90", "91", "92")} == {"device_id", "device_user_id", "employee_id"}


class TestProtocolMappings:
    def test_values_and_invalid_rows(self, imported):
        face = ProtocolMapping.objects.get(raw_value=15)
        assert (face.field, face.meaning_code, face.confidence, face.device_platform, face.firmware_version) == ("status", "FACE", "ASSUMED", "ZAM180_TFT", "Ver 6.60 Aug 19 2021")
        report = legacy_import.import_protocol_mappings([{"id": 90, "field": "colour", "raw_value": 1, "meaning_type": "VERIFY_MODE", "meaning_code": "X"}])
        assert report["created"] == 0 and report["violations"][0]["source_id"] == "90"


class TestAdmsEvidence:
    def test_recent_requests_with_the_receivers_redaction(self, imported):
        operlog = AdmsRequest.objects.get(request_kind="OPERLOG")
        body = bytes(operlog.body).decode()
        assert "Passwd=***" in body and "4321" not in body and "4321" not in operlog.body_text and operlog.extra["redacted"]
        assert operlog.extra["parsed"][0]["Passwd"] == "***" and operlog.extra["imported_from"] == "eSSL adms_requests"
        assert operlog.device.name == "PROJECT-01" and operlog.path == "/iclock/cdata"
        assert AdmsRequest.objects.filter(device__isnull=True).count() == 3  # the unknown serial and the no-serial requests

    def test_older_evidence_is_not_migrated(self, tables):
        hr_import.import_all(tables)
        legacy_import.import_devices(tables["devices"])
        report = legacy_import.import_adms_requests(tables["adms_requests"], now=CAPTURED + timedelta(days=31))
        assert (report["created"], report["skipped"]) == (0, 11) and not AdmsRequest.objects.exists()

    def test_biometric_bodies_are_dropped_and_odd_rows_survive(self, imported):
        rows = [
            {"id": 90, "received_at": "2026-09-29T10:00:00+00:00", "method": "POST", "path": "/iclock/cdata", "request_kind": "ATTPHOTO", "body": "\u00ff\u00d8photo", "body_bytes": 7},
            {
                "id": 91,
                "received_at": "2026-09-29T10:00:01+00:00",
                "method": "post",
                "path": "/iclock/x",
                "request_kind": "SOMETHING",
                "body_text": "TMP=AAAA",
                "headers": {"Authorization": "Bearer x"},
            },
        ]
        report = legacy_import.import_adms_requests(rows, now=CAPTURED)
        assert report["created"] == 2
        photo, odd = AdmsRequest.objects.filter(extra__imported_from="eSSL adms_requests").order_by("-id")[:2][::-1]
        assert photo.request_kind == "ATTPHOTO" and photo.body is None and photo.body_text == ""
        assert odd.request_kind == "UNKNOWN" and odd.method == "POST" and odd.body_text == "TMP=***" and "Authorization" not in odd.headers

    def test_quarantine_list(self, imported):
        entry = AdmsUnknownDevice.objects.get()
        assert (entry.serial_number, entry.request_count, entry.last_reason, entry.last_source_ip) == ("ZZZ0000000001", 2, "UNKNOWN_SERIAL", "127.0.0.1")
        assert legacy_import.import_adms_unknown_devices([{"id": 9, "serial_number": " "}])["violations"][0]["field"] == "serial_number"

    def test_quarantine_excerpts_get_the_receivers_redaction(self, imported):
        """eSSL kept the first 4000 characters of whatever an unknown serial pushed, USERINFO passwords included."""
        excerpt = "PIN=5\tName=Elena\tPri=0\tPasswd=2468\tCard=77\nPIN=6\tFID=0\tTMP=AAAAQUJD"
        row = {"id": 10, "serial_number": "ZZZ0000000077", "request_count": 3, "last_body_excerpt": excerpt, "first_seen_at": "2026-09-28T10:00:00+00:00", "last_seen_at": "2026-09-29T10:00:00+00:00"}
        assert legacy_import.import_adms_unknown_devices([row])["created"] == 1
        stored = AdmsUnknownDevice.objects.get(serial_number="ZZZ0000000077").last_body_excerpt
        assert "2468" not in stored and "AAAAQUJD" not in stored and "Passwd=***" in stored and "Name=Elena" in stored
