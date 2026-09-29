"""Health is derived from timestamps only (no is_online column): devices, agents, offices."""

from datetime import timedelta

import pytest
from django.utils import timezone

from devices.models import Agent, Device
from devices.services import health
from devices.tests.factories import AgentFactory, DeviceFactory

pytestmark = pytest.mark.django_db


def ago(seconds):
    return timezone.now() - timedelta(seconds=seconds)


class TestDevice:
    @pytest.mark.parametrize("age, state", [(None, "NEVER_SEEN"), (10, "ONLINE"), (600, "DEGRADED"), (3600, "OFFLINE")])
    def test_agent_delivered_grades_on_last_seen(self, age, state):
        device = DeviceFactory(last_seen_at=None if age is None else ago(age))
        assert health.connection_state(device) == state and health.transport(device) == "UNASSIGNED"

    def test_a_dead_agent_cannot_keep_its_devices_online(self):
        agent = AgentFactory(last_heartbeat_at=ago(3 * 3600))
        device = DeviceFactory(agent=agent, last_seen_at=ago(3 * 3600))
        assert health.agent_status(agent) == "OFFLINE" and health.connection_state(device) == "OFFLINE" and health.device_status(device) == "OFFLINE"

    def test_a_slow_agent_widens_its_devices_window(self):
        agent = AgentFactory(heartbeat_interval_seconds=600, offline_after_seconds=1200)
        device = DeviceFactory(agent=agent, last_seen_at=ago(1000))
        assert health.connection_state(device) == "ONLINE"
        assert health.connection_state(DeviceFactory(last_seen_at=ago(1000))) == "OFFLINE"

    def test_a_pushing_terminal_is_judged_on_its_pushes_only(self):
        dead = AgentFactory(last_heartbeat_at=ago(99999))
        device = DeviceFactory(agent=dead, adms_enabled=True, adms_token_hash="a" * 64, adms_last_seen_at=ago(5), last_seen_at=None)
        assert health.transport(device) == "ADMS_PUSH" and health.connection_state(device) == "ONLINE" and health.adms_state(device) == "ONLINE"
        quiet = DeviceFactory(agent=AgentFactory(last_heartbeat_at=ago(1)), adms_enabled=True, adms_token_hash="b" * 64, last_seen_at=ago(1))
        assert health.connection_state(quiet) == "NEVER_SEEN" and health.device_status(quiet) == "UNVERIFIED"
        assert health.adms_state(DeviceFactory()) == "UNKNOWN"

    def test_a_mismatch_outranks_everything(self):
        device = DeviceFactory(identity_status=Device.IdentityStatus.IDENTITY_MISMATCH, last_seen_at=ago(1))
        assert health.connection_state(device) == "IDENTITY_MISMATCH" and health.device_status(device) == "IDENTITY_MISMATCH"

    def test_status_and_block(self):
        device = DeviceFactory(identity_status=Device.IdentityStatus.UNVERIFIED, last_seen_at=ago(1), ip_address=None, name="MARS-01", serial_number="NCD1", expected_serial="NCD1")
        block = health.device_block(device)
        assert block["status"] == "UNVERIFIED" and block["is_connected"] and block["awaiting_discovery"] and block["display_label"] == "MARS-01 · NCD1"
        assert block["mapping_consistent"] is True and block["seconds_since_contact"] <= 2

    def test_crossed_filing_is_reported(self):
        agent = AgentFactory()
        device = DeviceFactory(agent=agent)
        assert health.mapping(device)["mapping_consistent"] is False and agent.code in health.mapping(device)["mapping_note"]


class TestAgent:
    def test_statuses(self):
        agent = AgentFactory(last_heartbeat_at=ago(10))
        assert health.agent_status(agent) == "ONLINE"
        for change in ({"last_error": "boom"}, {"failed_uploads": 1}, {"queued_records": 501}):
            degraded = AgentFactory(last_heartbeat_at=ago(10), **change)
            assert health.agent_status(degraded) == "DEGRADED"
        assert health.agent_status(AgentFactory(last_heartbeat_at=None)) == "OFFLINE"
        assert health.agent_status(AgentFactory(with_credential=False)) == "REVOKED"
        disabled = AgentFactory(last_heartbeat_at=ago(1))
        Agent.objects.filter(pk=disabled.pk).update(is_active=False)
        disabled.refresh_from_db()
        assert health.agent_status(disabled) == "REVOKED"


class TestOffice:
    def test_no_agent_awaiting_online_revoked(self):
        assert health.office_connection(None, [])["status"] == "NO_AGENT"
        agent = AgentFactory(last_heartbeat_at=ago(1))
        waiting = DeviceFactory(agent=agent, office=agent.office, ip_address=None)
        assert health.office_connection(agent, [waiting])["status"] == "AWAITING_DEVICE"
        found = DeviceFactory(agent=agent, office=agent.office, last_seen_at=ago(1))
        connection = health.office_connection(agent, [waiting, found])
        assert connection["status"] == "ONLINE" and connection["devices_online"] == 1 and connection["transport"] == "AGENT"
        assert health.office_connection(AgentFactory(with_credential=False), [found])["status"] == "REVOKED"

    def test_pushing_offices_ignore_the_agent(self):
        pushing = DeviceFactory(adms_enabled=True, adms_token_hash="c" * 64, adms_last_seen_at=ago(1))
        assert health.office_connection(None, [pushing])["status"] == "ONLINE"
        never = DeviceFactory(adms_enabled=True, adms_token_hash="d" * 64)
        assert health.office_connection(None, [never])["status"] == "AWAITING_DEVICE"

    def test_the_healthiest_serving_agent_speaks_for_the_office(self):
        filed = AgentFactory(last_heartbeat_at=ago(1))
        dead = AgentFactory(office=filed.office, last_heartbeat_at=None)
        live = AgentFactory(office=filed.office, last_heartbeat_at=ago(1))
        devices = [DeviceFactory(agent=dead, office=filed.office), DeviceFactory(agent=live, office=filed.office)]
        assert health.office_agents(filed.office_id, devices, [filed, dead, live]) == sorted([dead, live], key=lambda agent: agent.code)
        assert health.office_agent(filed.office_id, devices, [filed]) == live
        assert health.office_agents(filed.office_id, [], [filed]) == [filed]
