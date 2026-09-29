"""devices/agents/ — credentials shown once, rotation, revocation, one-time config download, delete guard, logs."""

from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from audit.models import AuditLog
from core.models import ServiceCredential
from devices.models import Agent
from devices.tests.conftest import agent_client_for
from devices.tests.factories import AgentFactory, DeviceFactory, SyncLogFactory
from hr.tests.factories import OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/devices/agents/"
AGENT_CONFIG = "/api/agent/v1/config/"


def detail(agent, suffix=""):
    return f"{URL}{agent.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        agent = AgentFactory()
        for method, path in [("get", URL), ("post", URL), ("patch", detail(agent)), ("post", detail(agent, "rotate-token/")), ("get", detail(agent, "config-download/"))]:
            assert getattr(api_client, method)(path).status_code == 401

    def test_credentials_need_manage(self, hr_client, viewer_client):
        agent = AgentFactory()
        assert hr_client.get(URL).status_code == 200 and hr_client.get(detail(agent, "logs/")).status_code == 200
        for client in (hr_client, viewer_client):
            assert client.post(URL, {"code": "X", "name": "X"}, format="json").status_code == 403
            assert client.post(detail(agent, "rotate-token/")).status_code == 403
            assert client.post(detail(agent, "revoke/")).status_code == 403
            assert client.get(detail(agent, "config-download/"), {"download": "x"}).status_code == 403
            assert client.delete(detail(agent)).status_code == 403
        assert viewer_client.patch(detail(agent), {"name": "x"}, format="json").status_code == 403

    def test_a_staff_token_is_not_an_agent_token(self, admin_client):
        assert admin_client.get(AGENT_CONFIG).status_code == 401


class TestCreateAndCredential:
    def test_create_returns_the_token_once_and_it_works(self, admin_client, admin_user):
        office = OfficeFactory()
        response = admin_client.post(URL, {"code": "OFFICE-001-AGENT", "name": "Reception PC", "office": str(office.uid)}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        token = body["token"]
        assert token.startswith("fl_") and body["agent"]["token_prefix"] == token.split("_")[1] and body["config_download"]["download"]
        assert body["agent"]["status"] == "OFFLINE"  # never heard from yet
        assert agent_client_for(type("A", (), {"token": token})).get(AGENT_CONFIG).status_code == 200
        detail_body = admin_client.get(detail(Agent.objects.get(code="OFFICE-001-AGENT"))).json()
        assert token not in str(detail_body) and detail_body["token_prefix"] == body["agent"]["token_prefix"]
        entries = AuditLog.objects.filter(action__in=["devices.agent_created", "core.service_credential_issued"])
        assert entries.count() == 2 and token not in str([entry.after for entry in entries])

    def test_validation_and_unique_code(self, admin_client):
        AgentFactory(code="OFFICE-001-AGENT")
        clash = admin_client.post(URL, {"code": "OFFICE-001-AGENT", "name": "x"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "agent_code_taken"
        bad = admin_client.post(URL, {"code": "", "name": "x", "heartbeat_interval_seconds": 5, "settings": [1]}, format="json")
        assert bad.status_code == 400 and {"code", "heartbeat_interval_seconds", "settings"} <= set(bad.json()["errors"])
        short = admin_client.post(URL, {"code": "A2", "name": "x", "heartbeat_interval_seconds": 120, "offline_after_seconds": 60}, format="json")
        assert short.status_code == 400 and "offline_after_seconds" in short.json()["errors"]

    def test_rotate_invalidates_the_old_token(self, admin_client):
        agent = AgentFactory()
        old = agent_client_for(agent)
        assert old.get(AGENT_CONFIG).status_code == 200
        response = admin_client.post(detail(agent, "rotate-token/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200
        assert old.get(AGENT_CONFIG).status_code == 401
        assert agent_client_for(type("A", (), {"token": response.json()["token"]})).get(AGENT_CONFIG).status_code == 200
        assert admin_client.post(detail(agent, "rotate-token/"), {"expected_version": 1}, format="json").status_code == 409

    def test_revoke_then_rotate_issues_a_fresh_credential(self, admin_client):
        agent = AgentFactory()
        client = agent_client_for(agent)
        response = admin_client.post(detail(agent, "revoke/"), {}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "REVOKED" and response.json()["token_revoked_at"]
        assert client.get(AGENT_CONFIG).status_code == 401
        rotated = admin_client.post(detail(agent, "rotate-token/"), {}, format="json").json()
        assert rotated["agent"]["is_active"] is True and rotated["agent"]["status"] == "OFFLINE"
        assert ServiceCredential.objects.filter(bound_object_id=agent.pk).count() == 2
        assert agent_client_for(type("A", (), {"token": rotated["token"]})).get(AGENT_CONFIG).status_code == 200

    def test_a_disabled_agent_is_refused_even_with_a_valid_token(self):
        agent = AgentFactory()
        Agent.objects.filter(pk=agent.pk).update(is_active=False)
        assert agent_client_for(agent).get(AGENT_CONFIG).status_code == 401


class TestConfigDownload:
    def test_single_use_agent_ini(self, admin_client):
        body = admin_client.post(URL, {"code": "OFFICE-009-AGENT", "name": "PC", "heartbeat_interval_seconds": 90}, format="json").json()
        agent = Agent.objects.get(code="OFFICE-009-AGENT")
        response = admin_client.get(detail(agent, "config-download/"), {"download": body["config_download"]["download"]})
        assert response.status_code == 200 and response["Content-Type"].startswith("text/plain") and response["Cache-Control"] == "private, no-store"
        text = response.content.decode()
        assert f"token = {body['token']}" in text and "agent_code = OFFICE-009-AGENT" in text and "heartbeat_interval = 90" in text and "api_version = v1" in text
        again = admin_client.get(detail(agent, "config-download/"), {"download": body["config_download"]["download"]})
        assert again.status_code == 410 and again.json()["code"] == "download_expired"

    def test_expired_foreign_or_missing_download(self, admin_client):
        body = admin_client.post(URL, {"code": "OFFICE-010-AGENT", "name": "PC"}, format="json").json()
        other = AgentFactory()
        assert admin_client.get(detail(other, "config-download/"), {"download": body["config_download"]["download"]}).status_code == 410
        assert admin_client.get(detail(other, "config-download/")).status_code == 400
        agent = Agent.objects.get(code="OFFICE-010-AGENT")
        fresh = admin_client.post(detail(agent, "rotate-token/"), {}, format="json").json()
        with freeze_time(timezone.now() + timedelta(minutes=11)):
            from django.core.cache import cache

            cache.clear()  # LocMem honours the TTL; clearing stands in for the expiry
            assert admin_client.get(detail(agent, "config-download/"), {"download": fresh["config_download"]["download"]}).status_code == 410


class TestListUpdateDelete:
    def test_list_embeds_devices_without_n_plus_one(self, admin_client, django_assert_max_num_queries):
        for _ in range(4):
            agent = AgentFactory()
            DeviceFactory.create_batch(2, agent=agent, office=agent.office)
        with django_assert_max_num_queries(8):
            rows = admin_client.get(URL).json()["results"]
        assert len(rows) == 4 and all(len(row["devices"]) == 2 for row in rows)
        assert all(row["office_mismatch"] is False for row in rows)

    def test_office_filter_finds_the_agent_serving_the_office(self, admin_client):
        ho, sales = OfficeFactory(), OfficeFactory()
        filed = AgentFactory(office=ho)
        serving = AgentFactory(office=ho)
        DeviceFactory(office=sales, agent=serving)
        found = {row["code"] for row in admin_client.get(URL, {"office": str(sales.uid)}).json()["results"]}
        assert found == {serving.code} and filed.code not in found
        row = admin_client.get(detail(serving)).json()
        assert row["office_mismatch"] is True and row["device_office_names"] == [sales.name]

    def test_patch_intervals(self, admin_client):
        agent = AgentFactory()
        response = admin_client.patch(detail(agent), {"heartbeat_interval_seconds": 30, "sync_interval_seconds": 120, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["heartbeat_interval_seconds"] == 30 and response.json()["version"] == 2
        bad = admin_client.patch(detail(agent), {"heartbeat_interval_seconds": 600}, format="json")  # above offline_after (300)
        assert bad.status_code == 400 and "offline_after_seconds" in bad.json()["errors"]
        assert admin_client.patch(detail(agent), {"name": "x", "expected_version": 1}, format="json").status_code == 409

    def test_delete_guard_and_soft_delete(self, admin_client):
        busy = AgentFactory()
        DeviceFactory(agent=busy)
        response = admin_client.delete(detail(busy))
        assert response.status_code == 409 and response.json()["code"] == "agent_in_use"
        idle = AgentFactory()
        client = agent_client_for(idle)
        assert admin_client.delete(detail(idle)).status_code == 204
        assert Agent.all_objects.get(pk=idle.pk).deleted_at is not None
        assert client.get(AGENT_CONFIG).status_code == 401

    def test_logs(self, admin_client):
        agent = AgentFactory()
        SyncLogFactory.create_batch(3, agent=agent)
        data = admin_client.get(detail(agent, "logs/"), {"status": "SUCCESS"}).json()
        assert len(data["results"]) == 3
