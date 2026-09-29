"""What devices provides to other screens: hr device-mappings, dependencies, office summary and deletion guard,
the dashboard counters and the weekly ops report section."""

from datetime import timedelta

import pytest
from django.utils import timezone

from core import ops_report
from devices.models import Device
from devices.tests.factories import AgentFactory, DeviceFactory, DeviceUserFactory, users_log
from hr import registries
from hr.tests.factories import EmployeeFactory, OfficeFactory

pytestmark = pytest.mark.django_db
EMPLOYEES = "/api/v1/hr/employees/"
OFFICES = "/api/v1/hr/offices/"


def test_the_providers_are_installed():
    assert registries.device_mappings.available and registries.device_reconciler.available
    assert "devices" in registries.office_summary.names() and "devices" in registries.employee_dependencies.names() and "devices" in registries.office_dependencies.names()
    assert "devices" in ops_report.registered()


class TestEmployeeSide:
    def test_device_mappings_per_device_and_presence(self, hr_client):
        employee = EmployeeFactory()
        read, unread = DeviceFactory(name="MARS-01"), DeviceFactory(name="SALES-01")
        users_log(read, ["7"])
        DeviceUserFactory(device=read, pin="7", employee=employee)
        DeviceUserFactory(device=unread, pin="7", employee=employee)
        body = hr_client.get(f"{EMPLOYEES}{employee.uid}/device-mappings/").json()
        assert body["available"] is True and [row["device"]["name"] for row in body["mappings"]] == ["MARS-01", "SALES-01"]
        assert [row["device_state"] for row in body["mappings"]] == ["ACTIVE_ON_DEVICE", "PENDING_SYNC"]
        assert body["details"]["presence"] == "ON_DEVICE" and body["details"]["active_on_devices"] == 1 and body["details"]["unconfirmed_devices"] == 1

    def test_mappings_are_history(self, hr_client):
        employee = EmployeeFactory()
        DeviceUserFactory(employee=employee)
        body = hr_client.get(f"{EMPLOYEES}{employee.uid}/dependencies/").json()
        assert body["counts"]["device_mappings"] == 1 and body["can_delete"] is False


class TestOfficeSide:
    def test_devices_and_agents_block_deleting_an_office(self, admin_client):
        office = OfficeFactory()
        AgentFactory(office=office)
        DeviceFactory(office=office)
        response = admin_client.delete(f"{OFFICES}{office.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "office_in_use" and set(response.json()["errors"]) == {"devices", "agents"}

    def test_summary_section_only_for_device_viewers(self, hr_client, auth_client, make_user):
        agent = AgentFactory(last_heartbeat_at=timezone.now())
        DeviceFactory(office=agent.office, agent=agent, last_seen_at=timezone.now(), name="MARS-01")
        section = hr_client.get(f"{OFFICES}{agent.office.uid}/summary/").json()["sections"]["devices"]
        assert section["connection"]["status"] == "ONLINE" and section["connection"]["agent"]["code"] == agent.code
        assert [device["name"] for device in section["devices"]] == ["MARS-01"] and section["devices"][0]["connection_state"] == "ONLINE"
        blind = auth_client(make_user(grants={"hr_setup": ["view"]}))
        assert "devices" not in blind.get(f"{OFFICES}{agent.office.uid}/summary/").json()["sections"]


class TestDashboardAndOps:
    def test_dashboard_counters(self, auth_client, make_user):
        client = auth_client(make_user(grants={"dashboard": ["view"], "devices": ["view"]}))
        DeviceFactory(last_seen_at=timezone.now())
        DeviceFactory(identity_status=Device.IdentityStatus.IDENTITY_MISMATCH)
        DeviceUserFactory()
        AgentFactory()
        counters = client.get("/api/v1/dashboard/").json()["modules"]["devices"]
        assert counters["devices"] == 3 and counters["devices_online"] == 1 and counters["devices_identity_mismatch"] == 1 and counters["agents_offline"] == 1
        assert counters["unlinked_device_users"] == 1
        other = auth_client(make_user(grants={"dashboard": ["view"]}))
        assert "devices" not in other.get("/api/v1/dashboard/").json()["modules"]

    def test_ops_report_section(self):
        AgentFactory(code="SILENT", last_heartbeat_at=timezone.now() - timedelta(hours=5))
        DeviceFactory(identity_status=Device.IdentityStatus.IDENTITY_MISMATCH)
        section = ops_report.build()["sections"]["devices"]
        assert section["agents"][0]["code"] == "SILENT" and section["agents"][0]["status"] == "OFFLINE" and 4.9 <= section["agents"][0]["offline_hours"] <= 5.1
        assert section["devices_identity_mismatch"] == 1 and section["adms_unknown_devices_seen"] == 0
