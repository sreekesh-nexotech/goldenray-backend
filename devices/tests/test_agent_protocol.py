"""/api/agent/v1/ — the office-agent protocol: scope, announce (never re-homes), truthful health, uploads."""

from datetime import timedelta

import pytest
from django.utils import timezone

from audit.models import AuditLog
from devices.models import Device, DeviceUser, SyncLog
from devices.services import health, punch_sink
from devices.tests.conftest import agent_client_for, events
from devices.tests.factories import AgentFactory, DeviceFactory, DeviceUserFactory
from hr.tests.factories import EmployeeFactory, OfficeFactory

pytestmark = pytest.mark.django_db
API = "/api/agent/v1/"
MARS = {"serial_number": "NCD8253601138", "ip_address": "192.168.1.209", "mac_address": "00:17:61:12:9c:49", "firmware_version": "Ver 6.60 Aug 19 2021", "platform": "ZAM180_TFT", "model": "x 2008"}


def punch(pin, time, uid=None, status=15, code=255):
    return {"device_record_uid": uid, "pin": pin, "device_time": time, "status": status, "punch": code, "raw_payload": {"uid": uid}}


class TestAuthentication:
    def test_no_token_bad_token_revoked_token(self, api_client, agent):
        assert api_client.get(f"{API}config/").status_code == 401
        api_client.credentials(HTTP_AUTHORIZATION="Bearer fl_000000000000_nope")
        assert api_client.get(f"{API}config/").status_code == 401
        agent.credential.revoked_at = timezone.now()
        agent.credential.save()
        assert agent_client_for(agent).get(f"{API}config/").status_code == 401

    def test_every_endpoint_needs_the_token(self, api_client):
        for method, path in [
            ("get", "config/"),
            ("post", "heartbeat/"),
            ("post", "devices/announce/"),
            ("post", "devices/identity-mismatch/"),
            ("post", "devices/discovery/"),
            ("post", "sync/users/"),
            ("post", "sync/attendance/"),
            ("get", "sync-status/"),
        ]:
            assert getattr(api_client, method)(f"{API}{path}").status_code == 401, path

    def test_throttle_scope_is_agent(self):
        from devices.views.agent_api import AgentProtocolView

        assert AgentProtocolView.throttle_scope == "agent"


class TestConfig:
    def test_config_lists_only_this_agents_active_devices(self, agent_client, agent):
        mine = DeviceFactory(agent=agent, comm_password=1234, ip_address=None, expected_serial="NCD1", serial_number=None)
        DeviceFactory(agent=agent, is_active=False)
        DeviceFactory(agent=AgentFactory())
        body = agent_client.get(f"{API}config/").json()
        assert body["agent"]["code"] == agent.code and body["intervals"] == {"heartbeat_seconds": 60, "sync_seconds": 300}
        assert body["announce_interval_seconds"] == 3600 and body["upload_batch_size"] == 200
        [device] = body["devices"]
        assert device["uid"] == str(mine.uid) and device["comm_password"] == 1234 and device["awaiting_discovery"] is True and device["expected_serial"] == "NCD1"


class TestHeartbeat:
    def test_reachable_moves_contact_unreachable_does_not(self, agent_client, agent):
        seen = DeviceFactory(agent=agent, last_seen_at=None)
        lost = DeviceFactory(agent=agent, last_seen_at=timezone.now() - timedelta(hours=2))
        body = {
            "version": "2.0.0",
            "hostname": "HO-PC",
            "local_ip": "192.168.1.20",
            "queued_records": 12,
            "devices": [
                {"device": str(seen.uid), "reachable": True, "device_time": (timezone.now() - timedelta(seconds=84)).astimezone(timezone.get_current_timezone()).strftime("%Y-%m-%dT%H:%M:%S")},
                {"serial_number": lost.serial_number, "reachable": False, "last_error": "TCP 4370 timed out"},
                {"serial_number": "NOT-MINE", "reachable": True},
            ],
        }
        response = agent_client.post(f"{API}heartbeat/", body, format="json")
        assert response.status_code == 200 and response.json()["agent"]["code"] == agent.code
        seen.refresh_from_db()
        lost.refresh_from_db()
        agent.refresh_from_db()
        assert seen.last_seen_at is not None and health.connection_state(seen) == health.ONLINE and -90 <= seen.clock_offset_seconds <= -80
        assert lost.last_error == "TCP 4370 timed out" and health.connection_state(lost) == health.OFFLINE
        assert agent.agent_version == "2.0.0" and agent.queued_records == 12 and str(agent.local_ip) == "192.168.1.20" and agent.last_device_contact_at is not None
        assert health.agent_status(agent) == health.ONLINE and seen.version == 1  # telemetry never bumps the version

    def test_a_dead_agent_ages_its_devices(self, agent):
        device = DeviceFactory(agent=agent, last_seen_at=timezone.now() - timedelta(minutes=20))
        assert health.connection_state(device) == health.OFFLINE  # eSSL kept is_online=True forever


class TestAnnounce:
    def test_binds_the_label_registered_device_and_verifies_it(self, agent_client, agent):
        device = DeviceFactory(serial_number=None, expected_serial=MARS["serial_number"], ip_address=None, agent=None, office=agent.office, identity_status=Device.IdentityStatus.UNVERIFIED)
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 200, response.json()
        assert response.json() == {"device": str(device.uid), "name": device.name, "serial_number": MARS["serial_number"], "office": response.json()["office"], "created": False}
        device.refresh_from_db()
        assert device.agent_id == agent.pk and device.serial_number == MARS["serial_number"] and str(device.ip_address) == MARS["ip_address"]
        assert device.identity_status == "VERIFIED" and device.expected_mac == MARS["mac_address"] and device.platform == "ZAM180_TFT"
        assert AuditLog.objects.filter(action="devices.device_bound", object_uid=device.uid).exists()

    def test_creates_an_unknown_terminal_in_the_agents_office(self, agent_client, agent):
        response = agent_client.post(f"{API}devices/announce/", {**MARS, "name": "MARS-01"}, format="json")
        assert response.status_code == 200 and response.json()["created"] is True
        device = Device.objects.get(serial_number=MARS["serial_number"])
        assert device.agent_id == agent.pk and device.office_id == agent.office_id and device.expected_serial == MARS["serial_number"]

    def test_never_rehomes_a_device_bound_to_another_agent(self, agent_client, agent):
        owner = AgentFactory()
        device = DeviceFactory(serial_number=MARS["serial_number"], expected_serial=MARS["serial_number"], agent=owner, office=owner.office)
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 409 and response.json()["code"] == "device_bound_elsewhere"
        device.refresh_from_db()
        assert device.agent_id == owner.pk and device.office_id == owner.office_id
        refusal = SyncLog.objects.get(device=device, details__event="DEVICE_BOUND_ELSEWHERE")
        assert refusal.agent_id == agent.pk and AuditLog.objects.filter(action="devices.device_announce_refused").exists()

    def test_does_not_adopt_an_unbound_device_of_another_office(self, agent_client, agent):
        device = DeviceFactory(serial_number=MARS["serial_number"], expected_serial=MARS["serial_number"], agent=None, office=OfficeFactory())
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 409 and response.json()["code"] == "device_bound_elsewhere"
        device.refresh_from_db()
        assert device.agent_id is None

    def test_a_serial_against_the_pin_is_a_recorded_mismatch(self, agent_client, agent):
        device = DeviceFactory(serial_number=None, expected_serial="NCD0000000001", ip_address="192.168.1.210", agent=agent)
        response = agent_client.post(f"{API}devices/announce/", {**MARS, "serial_number": "NCD9999999999", "ip_address": "192.168.1.210"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "identity_mismatch"
        device.refresh_from_db()
        assert device.identity_status == "IDENTITY_MISMATCH" and "NCD9999999999" in device.identity_message  # recorded although refused
        assert health.connection_state(device) == health.IDENTITY_MISMATCH

    def test_a_mac_against_the_known_one_is_a_mismatch(self, agent_client, agent):
        DeviceFactory(serial_number=MARS["serial_number"], expected_serial=MARS["serial_number"], expected_mac="00:17:61:00:00:01", agent=agent)
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 409 and response.json()["code"] == "identity_mismatch"

    def test_same_address_another_serial_is_another_device(self, agent_client, agent):
        known = DeviceFactory(serial_number="NCD1", expected_serial="NCD1", ip_address=MARS["ip_address"], agent=agent)
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 200 and response.json()["created"] is True
        known.refresh_from_db()
        assert known.identity_status == "IDENTITY_MISMATCH"

    def test_an_inactive_device_is_refused(self, agent_client, agent):
        DeviceFactory(serial_number=MARS["serial_number"], expected_serial=MARS["serial_number"], agent=agent, is_active=False)
        response = agent_client.post(f"{API}devices/announce/", MARS, format="json")
        assert response.status_code == 409 and response.json()["code"] == "device_inactive"

    def test_validation(self, agent_client):
        response = agent_client.post(f"{API}devices/announce/", {"ip_address": "999.9.9.9"}, format="json")
        assert response.status_code == 400 and {"serial_number", "ip_address"} <= set(response.json()["errors"])


class TestIdentityAndDiscovery:
    def test_identity_mismatch_report(self, agent_client, agent):
        device = DeviceFactory(serial_number=None, expected_serial="NCD0000000001", agent=agent)
        body = {"expected_serial": "NCD0000000001", "reported_serial": "NCD9", "ip_address": "192.168.1.210", "reason": "another terminal answered"}
        response = agent_client.post(f"{API}devices/identity-mismatch/", body, format="json")
        assert response.status_code == 200 and response.json()["device"] == str(device.uid) and response.json()["identity_status"] == "IDENTITY_MISMATCH"
        device.refresh_from_db()
        assert device.identity_status == "IDENTITY_MISMATCH" and device.expected_serial == "NCD0000000001"
        unknown = agent_client.post(f"{API}devices/identity-mismatch/", {"expected_serial": "NOPE"}, format="json").json()
        assert unknown["device"] is None and unknown["recorded"] is True

    def test_discovery_locates_by_serial_only(self, agent_client, agent):
        label = DeviceFactory(serial_number=None, expected_serial="NCD8252101212", expected_mac="00:17:61:12:f2:d1", ip_address=None, agent=agent)
        guarded = DeviceFactory(serial_number=None, expected_serial="NCD2", expected_mac="00:17:61:00:00:02", ip_address=None, agent=agent)
        waiting = DeviceFactory(serial_number=None, expected_serial="NCD3", ip_address=None, agent=agent)
        found = [
            {"ip_address": "192.168.1.60", "mac_address": "00:17:61:12:F2:D1", "serial_number": "NCD8252101212", "device_name": "x 2008", "firmware_version": "Ver 6.60"},
            {"ip_address": "192.168.1.61", "mac_address": "00:17:61:99:99:99", "serial_number": "NCD2"},
            {"ip_address": "192.168.1.203", "error": "timed out"},
        ]
        body = agent_client.post(f"{API}devices/discovery/", {"subnet": "192.168.1.0/24", "hosts_scanned": 254, "hosts_open": 3, "found": found}, format="json").json()
        assert [(match["device"], match["matched"]) for match in body["matches"]] == [(str(label.uid), True), (str(guarded.uid), False)]
        assert [host["ip_address"] for host in body["unmatched"]] == ["192.168.1.203"] and sorted(body["still_awaiting"]) == ["NCD2", "NCD3"]  # the MAC-guarded terminal is not located yet
        label.refresh_from_db()
        guarded.refresh_from_db()
        waiting.refresh_from_db()
        assert str(label.ip_address) == "192.168.1.60" and label.serial_number == "NCD8252101212" and label.identity_status == "VERIFIED"
        assert guarded.ip_address is None and guarded.identity_status == "IDENTITY_MISMATCH"
        assert SyncLog.objects.filter(details__event="LAN_DISCOVERY").count() == 1


class TestUsers:
    def test_whole_table_read_is_the_watermark(self, agent_client, agent):
        device = DeviceFactory(agent=agent)
        stale = DeviceUserFactory(device=device, pin="5")
        users = [{"pin": "1", "name": "Asha", "privilege": 14, "card": "123"}, {"pin": "EMP001", "name": "Binu"}, {"pin": "", "name": "broken"}]
        response = agent_client.post(f"{API}sync/users/", {"device": str(device.uid), "users": users}, format="json")
        assert response.status_code == 200 and response.json() == {"device": str(device.uid), "received": 3, "created": 2, "updated": 0, "invalid": 1}
        log = SyncLog.objects.get(device=device, sync_type="USERS")
        assert log.status == "PARTIAL" and log.details["users_present"] == ["1", "EMP001"]  # PARTIAL: not a watermark
        again = agent_client.post(f"{API}sync/users/", {"serial_number": device.serial_number, "users": users[:2]}, format="json").json()
        assert again["updated"] == 2 and again["created"] == 0
        assert DeviceUser.objects.filter(device=device).count() == 3  # never deleted
        from devices.services import roster

        states = roster.describe(DeviceUser.objects.filter(device=device).select_related("device", "employee"))
        assert states[stale.pk]["device_state"] == "MISSING_FROM_DEVICE"
        device.refresh_from_db()
        assert device.user_count == 2

    def test_only_this_agents_devices(self, agent_client):
        other = DeviceFactory(agent=AgentFactory())
        response = agent_client.post(f"{API}sync/users/", {"device": str(other.uid), "users": []}, format="json")
        assert response.status_code == 404 and response.json()["code"] == "device_not_found"
        assert agent_client.post(f"{API}sync/users/", {"users": []}, format="json").status_code == 400


class TestAttendance:
    def test_batch_is_stored_through_the_sink_and_announced(self, agent_client, agent, sink):
        device = DeviceFactory(agent=agent)
        employee = EmployeeFactory()
        DeviceUserFactory(device=device, pin="1", employee=employee)
        records = [punch("1", "2026-09-21T09:28:11", 101), punch("2", "2026-09-21T09:32:00", 102), punch("", "2026-09-21T10:00:00"), punch("3", "not a time")]
        response = agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "batch_id": "b1", "records": records}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json() == {"device": str(device.uid), "received": 4, "new": 2, "duplicate": 0, "invalid": 2, "discarded": 0}
        [event] = events("attendance.punches_ingested")
        assert event["employee_uids"] == [str(employee.uid)] and event["unmapped_pins"] == ["2"] and event["dates"] == ["2026-09-21"] and event["source"] == "AGENT_PUSH"
        device.refresh_from_db()
        assert device.attendance_count == 2 and device.last_punch_at is not None
        again = agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": records[:2]}, format="json").json()
        assert again["new"] == 0 and again["duplicate"] == 2 and len(events("attendance.punches_ingested")) == 1
        log = SyncLog.objects.filter(device=device, sync_type="ATTENDANCE").order_by("id").first()
        assert log.status == "PARTIAL" and log.details["batch_id"] == "b1"

    def test_without_a_punch_store_nothing_is_claimed_as_stored(self, agent_client, agent):
        device = DeviceFactory(agent=agent)
        body = agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": [punch("1", "2026-09-21T09:28:11")]}, format="json").json()
        assert body["new"] == 0 and body["discarded"] == 1 and not punch_sink.installed() and events("attendance.punches_ingested") == []

    def test_batches_are_capped_at_200(self, agent_client, agent):
        device = DeviceFactory(agent=agent)
        response = agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": [punch("1", "2026-09-21T09:00:00")] * 201}, format="json")
        assert response.status_code == 400 and "records" in response.json()["errors"]
        assert agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": []}, format="json").status_code == 400

    def test_idempotency_key_replays_the_first_answer(self, agent_client, agent, sink):
        device = DeviceFactory(agent=agent)
        body = {"device": str(device.uid), "records": [punch("1", "2026-09-21T09:28:11")]}
        first = agent_client.post(f"{API}sync/attendance/", body, format="json", HTTP_IDEMPOTENCY_KEY="batch-0001-abc")
        replay = agent_client.post(f"{API}sync/attendance/", body, format="json", HTTP_IDEMPOTENCY_KEY="batch-0001-abc")
        assert first.json()["new"] == 1 and replay.json() == first.json() and replay["Idempotent-Replayed"] == "true"
        reused = agent_client.post(f"{API}sync/attendance/", {**body, "batch_id": "other"}, format="json", HTTP_IDEMPOTENCY_KEY="batch-0001-abc")
        assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"

    def test_same_punch_over_both_transports_collapses(self, agent_client, agent, sink, adms_on, client):
        from devices.services import devices as device_service

        device = DeviceFactory(agent=agent, serial_number="NCD7", expected_serial="NCD7")
        device, token = device_service.enable_adms(device, user=None)
        agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": [punch("7", "2026-09-22T09:31:05", 5, status=15, code=0)]}, format="json")
        reply = client.post(f"/iclock/{token}/cdata?SN=NCD7&table=ATTLOG", data="7\t2026-09-22 09:31:05\t0\t15\t0\n", content_type="text/plain")
        assert reply.content == b"OK: 1" and len(sink.punches) == 1

    def test_sync_status(self, agent_client, agent, sink):
        device = DeviceFactory(agent=agent)
        agent_client.post(f"{API}sync/attendance/", {"device": str(device.uid), "records": [punch("1", "2026-09-21T09:28:11", 41), punch("1", "2026-09-21T18:00:00", 42)]}, format="json")
        body = agent_client.get(f"{API}sync-status/", {"device": str(device.uid)}).json()
        assert body["stored_records"] == 2 and body["highest_device_record_uid"] == 42 and body["latest_device_time"] == "2026-09-21T18:00:00" and body["punch_store_installed"] is True
        assert agent_client.get(f"{API}sync-status/", {"serial_number": "NOPE"}).status_code == 404
