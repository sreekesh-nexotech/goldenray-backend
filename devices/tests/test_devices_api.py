"""devices/ — validated CRUD, derived health, mapping report, logs, rehome, ADMS enable/disable, refresh."""

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from audit.models import AuditLog
from devices.models import Device, SyncLog
from devices.tests.factories import AgentFactory, DeviceFactory, DeviceUserFactory, SyncLogFactory
from hr.tests.factories import OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/devices/"


def detail(device, suffix=""):
    return f"{URL}{device.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        device = DeviceFactory()
        for method, path in [
            ("get", URL),
            ("post", URL),
            ("get", detail(device)),
            ("patch", detail(device)),
            ("delete", detail(device)),
            ("get", f"{URL}mapping/"),
            ("post", detail(device, "rehome/")),
            ("post", detail(device, "refresh-employees/")),
        ]:
            assert getattr(api_client, method)(path).status_code == 401, (method, path)

    def test_actions_follow_the_registry(self, viewer_client, hr_client):
        device = DeviceFactory()
        assert viewer_client.get(URL).status_code == 200
        assert viewer_client.get(detail(device, "logs/")).status_code == 200
        assert viewer_client.get(detail(device, "employee-reconciliation/")).status_code == 200
        assert viewer_client.post(URL, {"name": "X", "ip_address": "10.0.0.1"}, format="json").status_code == 403
        assert viewer_client.patch(detail(device), {"name": "X"}, format="json").status_code == 403
        assert viewer_client.post(detail(device, "refresh-employees/")).status_code == 403
        # HR (PLAN §3.2): devices view/sync — may refresh, may not manage
        assert hr_client.post(detail(device, "refresh-employees/")).status_code == 200
        assert hr_client.post(detail(device, "rehome/"), {"agent_uid": None, "reason": "x"}, format="json").status_code == 403
        assert hr_client.post(detail(device, "adms/enable/"), {}, format="json").status_code == 403
        assert hr_client.delete(detail(device)).status_code == 403

    def test_other_modules_do_not_open_devices(self, auth_client, make_user):
        assert auth_client(make_user(grants={"employees": "*", "attendance": "*"})).get(URL).status_code == 403


class TestCreate:
    def test_register_from_the_label(self, admin_client, admin_user):
        office, agent = OfficeFactory(), AgentFactory()
        body = {"name": "SALES-01", "serial_number": " NCD8252101212 ", "expected_mac": "00-17-61-12-F2-D1", "office": str(office.uid), "agent": str(agent.uid), "comm_password": 1234}
        response = admin_client.post(URL, body, format="json")
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["serial_number"] == "NCD8252101212" and data["expected_serial"] == "NCD8252101212"
        assert data["expected_mac"] == "00:17:61:12:f2:d1" and data["has_comm_password"] is True and "comm_password" not in data
        assert data["health"]["connection_state"] == "NEVER_SEEN" and data["health"]["transport"] == "AGENT_DELIVERED" and data["health"]["awaiting_discovery"] is True
        assert data["agent"]["code"] == agent.code and data["office"]["uid"] == str(office.uid)
        assert AuditLog.objects.get(action="devices.device_created").actor == admin_user

    def test_validation_envelope(self, admin_client):
        response = admin_client.post(URL, {"name": "", "ip_address": "999.1.1.1", "port": 0, "protocol": "ADMS_PUSH"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert {"name", "ip_address", "port", "protocol"} <= set(response.json()["errors"])

    def test_a_device_must_be_locatable(self, admin_client):
        response = admin_client.post(URL, {"name": "X"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "device_not_locatable"

    def test_invalid_mac(self, admin_client):
        response = admin_client.post(URL, {"name": "X", "ip_address": "10.0.0.1", "expected_mac": "00:17:61"}, format="json")
        assert response.status_code == 400 and "expected_mac" in response.json()["errors"]

    def test_a_serial_belongs_to_one_device(self, admin_client):
        DeviceFactory(serial_number="NCD1", expected_serial="NCD1")
        DeviceFactory(serial_number=None, expected_serial="NCD2")
        for body in ({"name": "A", "serial_number": "NCD1"}, {"name": "B", "expected_serial": "NCD2"}, {"name": "C", "expected_serial": "NCD1"}):
            response = admin_client.post(URL, body, format="json")
            assert response.status_code == 409 and response.json()["code"] == "device_serial_taken", body

    def test_a_revoked_agent_cannot_carry_a_new_device(self, admin_client):
        agent = AgentFactory(is_active=False)
        response = admin_client.post(URL, {"name": "X", "ip_address": "10.0.0.1", "agent": str(agent.uid)}, format="json")
        assert response.status_code == 400 and "agent" in response.json()["errors"]


class TestListAndDetail:
    def test_list_derives_health_without_n_plus_one(self, admin_client, django_assert_max_num_queries):
        agent = AgentFactory()
        now = timezone.now()
        for index in range(6):
            DeviceFactory(agent=agent if index % 2 else None, last_seen_at=now - timedelta(seconds=index * 200))
        with django_assert_max_num_queries(8):
            response = admin_client.get(URL)
        rows = response.json()["results"]
        assert len(rows) == 6
        assert {row["health"]["connection_state"] for row in rows} >= {"ONLINE", "DEGRADED", "OFFLINE"}

    def test_filters_and_search(self, admin_client):
        office, agent = OfficeFactory(), AgentFactory()
        DeviceFactory(office=office, agent=agent, name="MARS-01")
        DeviceFactory(is_active=False)
        DeviceFactory(adms_enabled=True, adms_token_hash="a" * 64)
        assert admin_client.get(URL, {"office": str(office.uid)}).json()["count"] == 1
        assert admin_client.get(URL, {"filter[agent]": str(agent.uid)}).json()["count"] == 1
        assert admin_client.get(URL, {"is_active": "false"}).json()["count"] == 1
        assert admin_client.get(URL, {"adms_enabled": "true"}).json()["count"] == 1
        assert admin_client.get(URL, {"unassigned": "true"}).json()["count"] == 2
        assert admin_client.get(URL, {"search": "MARS"}).json()["count"] == 1

    def test_detail_never_exposes_secrets(self, admin_client):
        device = DeviceFactory(comm_password=77, adms_enabled=True, adms_token_hash="b" * 64)
        data = admin_client.get(detail(device)).json()
        assert "comm_password" not in data and "adms_token_hash" not in data and data["adms_token_set"] is True

    def test_unknown_uid_is_404(self, admin_client):
        assert admin_client.get(f"{URL}00000000-0000-0000-0000-000000000000/").status_code == 404


class TestUpdateAndDelete:
    def test_patch_with_expected_version_and_audit(self, admin_client):
        device = DeviceFactory(name="Old")
        response = admin_client.patch(detail(device), {"name": "New", "expected_mac": "001761129C49", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2 and response.json()["expected_mac"] == "00:17:61:12:9c:49"
        entry = AuditLog.objects.get(action="devices.device_updated")
        assert entry.before["name"] == "Old" and entry.after["name"] == "New"

    def test_stale_version(self, admin_client):
        device = DeviceFactory(version=4)
        response = admin_client.patch(detail(device), {"name": "x", "expected_version": 3}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert admin_client.delete(f"{detail(device)}?expected_version=3").status_code == 409

    def test_patch_cannot_move_the_agent_or_the_serial(self, admin_client):
        agent = AgentFactory()
        device = DeviceFactory(serial_number="NCD9", expected_serial="NCD9")
        response = admin_client.patch(detail(device), {"agent": str(agent.uid), "serial_number": "OTHER"}, format="json")
        assert response.status_code == 200
        device.refresh_from_db()
        assert device.agent_id is None and device.serial_number == "NCD9" and device.version == 1

    def test_pinning_another_devices_serial_is_refused(self, admin_client):
        DeviceFactory(serial_number="NCD1", expected_serial="NCD1")
        device = DeviceFactory()
        response = admin_client.patch(detail(device), {"expected_serial": "NCD1"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "device_serial_taken"

    def test_telemetry_never_makes_an_edit_stale(self, admin_client, agent_client, agent):
        device = DeviceFactory(agent=agent)
        agent_client.post("/api/agent/v1/heartbeat/", {"devices": [{"device": str(device.uid), "reachable": True}]}, format="json")
        assert admin_client.patch(detail(device), {"name": "Renamed", "expected_version": 1}, format="json").status_code == 200

    def test_delete_without_history(self, admin_client):
        device = DeviceFactory(adms_enabled=True, adms_token_hash="c" * 64)
        assert admin_client.delete(detail(device)).status_code == 204
        row = Device.all_objects.get(pk=device.pk)
        assert row.deleted_at is not None and row.adms_token_hash is None and not row.adms_enabled

    def test_delete_is_refused_with_history(self, admin_client):
        device = DeviceFactory(attendance_count=3)
        DeviceUserFactory(device=device)
        response = admin_client.delete(detail(device))
        assert response.status_code == 409 and response.json()["code"] == "device_has_history"
        assert response.json()["errors"] == {"device_users": ["1"], "punches": ["3"]}


class TestRehome:
    def test_rehome_moves_the_device_and_audits_the_reason(self, admin_client):
        old, new = AgentFactory(), AgentFactory()
        device = DeviceFactory(agent=old)
        office = OfficeFactory()
        response = admin_client.post(detail(device, "rehome/"), {"agent_uid": str(new.uid), "office_uid": str(office.uid), "reason": "moved to the sales office", "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["agent"]["uid"] == str(new.uid) and response.json()["office"]["uid"] == str(office.uid)
        entry = AuditLog.objects.get(action="devices.device_rehomed")
        assert entry.note == "moved to the sales office" and entry.after["agent"] == str(new.uid)

    def test_rehome_to_no_agent_keeps_the_office(self, admin_client):
        device = DeviceFactory(agent=AgentFactory())
        office = device.office
        response = admin_client.post(detail(device, "rehome/"), {"agent_uid": None, "reason": "agent PC retired"}, format="json")
        assert response.status_code == 200 and response.json()["agent"] is None and response.json()["office"]["uid"] == str(office.uid)

    def test_rehome_validation(self, admin_client):
        device = DeviceFactory()
        assert admin_client.post(detail(device, "rehome/"), {"agent_uid": None, "reason": " "}, format="json").status_code == 400
        revoked = AgentFactory(is_active=False)
        response = admin_client.post(detail(device, "rehome/"), {"agent_uid": str(revoked.uid), "reason": "x"}, format="json")
        assert response.status_code == 400 and "agent_uid" in response.json()["errors"]
        assert admin_client.post(detail(device, "rehome/"), {"agent_uid": None, "reason": "x", "expected_version": 9}, format="json").status_code == 409


class TestAdms:
    def test_enable_issues_a_token_once(self, admin_client):
        device = DeviceFactory()
        response = admin_client.post(detail(device, "adms/enable/"), {"allowed_ips": ["59.88.139.169", "10.0.0.0/8"]}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        token = body["token"]
        assert body["iclock_path"] == f"/iclock/{token}/" and body["device"]["adms_enabled"] is True
        assert body["device"]["adms_allowed_ips"] == ["59.88.139.169/32", "10.0.0.0/8"]
        device.refresh_from_db()
        assert device.adms_token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(AuditLog.objects.get(action="devices.device_adms_enabled").after)
        again = admin_client.post(detail(device, "adms/enable/"), {}, format="json").json()
        assert again["token"] != token and again["device"]["adms_allowed_ips"] == ["59.88.139.169/32", "10.0.0.0/8"]

    def test_a_terminal_without_a_serial_cannot_be_given_a_push_token(self, admin_client):
        """The receiver admits serial AND token: a token for a device with no serial could only ever be quarantined."""
        device = DeviceFactory(serial_number=None, expected_serial=None, ip_address="192.168.1.50")
        response = admin_client.post(detail(device, "adms/enable/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "device_serial_required"
        device.refresh_from_db()
        assert device.adms_enabled is False and device.adms_token_hash is None and device.version == 1
        label_only = DeviceFactory(serial_number=None, expected_serial="NCD8252101212", ip_address=None)
        assert admin_client.post(detail(label_only, "adms/enable/"), {}, format="json").status_code == 200  # the pin is enough

    def test_enable_checks_the_version(self, admin_client):
        device = DeviceFactory(version=3)
        response = admin_client.post(detail(device, "adms/enable/"), {"expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        device.refresh_from_db()
        assert device.adms_token_hash is None and device.adms_enabled is False

    def test_invalid_allow_list(self, admin_client):
        response = admin_client.post(detail(DeviceFactory(), "adms/enable/"), {"allowed_ips": ["not-an-ip"]}, format="json")
        assert response.status_code == 400 and "allowed_ips" in response.json()["errors"]

    def test_disable_withdraws_the_token(self, admin_client):
        device = DeviceFactory(adms_enabled=True, adms_token_hash="d" * 64)
        response = admin_client.post(detail(device, "adms/disable/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["adms_enabled"] is False and response.json()["adms_token_set"] is False
        assert admin_client.post(detail(device, "adms/disable/"), {"expected_version": 1}, format="json").status_code == 409


class TestMappingAndLogs:
    def test_mapping_reports_crossed_filings(self, admin_client):
        ho, sales = OfficeFactory(name="Head Office"), OfficeFactory(name="Sales")
        agent = AgentFactory(office=ho)
        DeviceFactory(office=ho, agent=agent, name="MARS-01")
        crossed = DeviceFactory(office=sales, agent=agent, name="SALES-01")
        data = admin_client.get(f"{URL}mapping/").json()
        assert [row["uid"] for row in data["inconsistencies"]] == [str(crossed.uid)]
        assert data["inconsistencies"][0]["health"]["mapping_note"].startswith(agent.code)
        [row] = [row for row in data["agents"] if row["agent"]["uid"] == str(agent.uid)]
        assert row["serves_devices"] == ["MARS-01", "SALES-01"] and row["serves_offices"] == ["Head Office", "Sales"]
        assert data["truncated"] is False

    def test_logs_are_cursor_paginated_and_filtered(self, admin_client):
        device = DeviceFactory()
        for index in range(3):
            SyncLogFactory(device=device, sync_type=SyncLog.Type.ATTENDANCE if index else SyncLog.Type.USERS, started_at=timezone.now() - timedelta(minutes=index))
        SyncLogFactory()  # another device
        data = admin_client.get(detail(device, "logs/"), {"page_size": 2}).json()
        assert len(data["results"]) == 2 and data["next"] and "count" not in data
        assert admin_client.get(detail(device, "logs/"), {"sync_type": "USERS"}).json()["results"][0]["sync_type"] == "USERS"
