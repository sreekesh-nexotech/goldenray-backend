"""Reconciliation: one device (refresh-employees/, employee-reconciliation/, user-reconciliation/) and the whole estate
(hr/employees/reconcile-devices/, provided by devices). Nothing is ever deleted; the watermark is the last SUCCESS read."""

from datetime import datetime

import pytest

from audit.models import AuditLog
from devices.models import Device
from devices.services import roster
from devices.tests.factories import AgentFactory, DeviceFactory, DeviceUserFactory, SyncLogFactory, users_log
from hr.models import Employee
from hr.tests.factories import EmployeeFactory, OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/devices/"
RECONCILE = "/api/v1/hr/employees/reconcile-devices/"


def detail(device, suffix=""):
    return f"{URL}{device.uid}/{suffix}"


@pytest.fixture
def carried():
    agent = AgentFactory()
    return DeviceFactory(agent=agent, office=agent.office)


class TestRefreshEmployees:
    def test_asks_the_agent_and_answers_the_last_read(self, hr_client, carried):
        users_log(carried, ["1"])
        DeviceUserFactory(device=carried, pin="1", employee=EmployeeFactory())
        body = hr_client.post(detail(carried, "refresh-employees/")).json()
        assert body["refresh"]["requested"] is True and body["refresh"]["transport"] == "AGENT_DELIVERED" and body["transport"] == "AGENT_DELIVERED"
        assert body["summary"] == {"new": 0, "matched": 1, "missing": 0, "unknown": 0, "total": 1} and body["reconciliation"]["active_on_device"] == 1
        carried.refresh_from_db()
        assert carried.users_read_requested_at is not None and AuditLog.objects.filter(action="devices.device_users_read_requested").count() == 1

    def test_push_and_unassigned_terminals_cannot_be_read(self, admin_client, adms_on):
        from devices.services.devices import enable_adms

        pushing, _ = enable_adms(DeviceFactory(), user=None)
        lonely = DeviceFactory()
        assert admin_client.post(detail(pushing, "refresh-employees/")).json()["refresh"]["transport"] == "ADMS_PUSH"
        body = admin_client.post(detail(lonely, "refresh-employees/")).json()["refresh"]
        assert body["requested"] is False and body["transport"] == "UNASSIGNED" and "Rehome" in body["note"]
        assert Device.objects.filter(users_read_requested_at__isnull=False).count() == 0

    def test_inactive_device_and_permissions(self, admin_client, viewer_client, api_client, carried):
        assert api_client.post(detail(carried, "refresh-employees/")).status_code == 401
        assert viewer_client.post(detail(carried, "refresh-employees/")).status_code == 403
        Device.objects.filter(pk=carried.pk).update(is_active=False)
        response = admin_client.post(detail(carried, "refresh-employees/"))
        assert response.status_code == 409 and response.json()["code"] == "device_inactive"


class TestEmployeeReconciliation:
    def test_the_four_categories_of_one_device(self, viewer_client, carried):
        users_log(carried, ["1", "2", "3", "4"])
        EmployeeFactory(code="2")
        EmployeeFactory(full_name="Ravi Kumar")
        EmployeeFactory(full_name="Ravi Kumar")
        matched = DeviceUserFactory(device=carried, pin="1", employee=EmployeeFactory())
        DeviceUserFactory(device=carried, pin="2", name="Somebody")
        DeviceUserFactory(device=carried, pin="3", name="Ravi Kumar")
        DeviceUserFactory(device=carried, pin="4", name="Nobody Known")
        DeviceUserFactory(device=carried, pin="5", employee=EmployeeFactory())
        body = viewer_client.get(detail(carried, "employee-reconciliation/")).json()
        rows = {row["pin"]: row for row in body["rows"]}
        assert body["refresh"] is None and body["summary"] == {"new": 1, "matched": 1, "missing": 1, "unknown": 2, "total": 5}
        assert (rows["1"]["category"], rows["1"]["employee"]["uid"]) == ("ALREADY_MATCHED", str(matched.employee.uid))
        assert (rows["2"]["category"], rows["2"]["available_action"], rows["2"]["requires_confirmation"]) == ("NEW_ON_DEVICE", "LINK_EXISTING", True)
        assert rows["3"]["ambiguous"] is True and len(rows["3"]["candidate_employees"]) == 2 and rows["3"]["available_action"] == "IDENTIFY"
        assert rows["4"]["category"] == "UNKNOWN" and rows["4"]["candidate_employees"] == []
        assert rows["5"]["category"] == "MISSING_FROM_DEVICE" and "Nothing was deleted" in rows["5"]["status_note"]

    def test_user_reconciliation_counts_and_removals(self, viewer_client, carried):
        users_log(carried, ["1", "2"])
        leaver = EmployeeFactory(is_active=False)
        DeviceUserFactory(device=carried, pin="1", employee=leaver)
        DeviceUserFactory(device=carried, pin="2")
        DeviceUserFactory(device=carried, pin="3", employee=EmployeeFactory())
        body = viewer_client.get(detail(carried, "user-reconciliation/")).json()
        assert (body["total_mappings"], body["active_on_device"], body["missing_from_device"], body["deactivated_in_software"], body["unlinked"]) == (3, 2, 1, 1, 1)
        assert [item["pin"] for item in body["pending_device_removals"]] == ["1"] and body["device_deletion_supported"] is False and body["sync_state"] == "OK"

    def test_a_failed_read_after_a_good_one_keeps_the_good_watermark(self, viewer_client, carried):
        good = users_log(carried, ["1"])
        SyncLogFactory(device=carried, status="FAILED", error_message="timed out")
        DeviceUserFactory(device=carried, pin="1")
        body = viewer_client.get(detail(carried, "user-reconciliation/")).json()
        assert body["sync_state"] == roster.SYNC_LAST_FAILED and body["active_on_device"] == 1 and datetime.fromisoformat(body["users_last_confirmed_at"]) == good.started_at


@pytest.fixture
def estate():
    """Device A (read: 1–4) and B (never read); employees E001 (on A), code 2, E005 (gone from A), E009 (only on B), E010."""
    agent = AgentFactory()
    a = DeviceFactory(agent=agent, office=agent.office, name="A")
    b = DeviceFactory(name="B")
    users_log(a, ["1", "2", "3", "4"])
    people = {code: EmployeeFactory(code=code, office=agent.office) for code in ("E001", "2", "E005", "E009", "E010")}
    DeviceUserFactory(device=a, pin="1", employee=people["E001"])
    DeviceUserFactory(device=a, pin="2", name="Two")
    DeviceUserFactory(device=a, pin="3", name="New Person")
    DeviceUserFactory(device=a, pin="4", name="")
    DeviceUserFactory(device=a, pin="5", employee=people["E005"])
    DeviceUserFactory(device=b, pin="9", employee=people["E009"])
    return a, b, people


def codes(entries):
    return sorted(entry["employee"]["code"] for entry in entries)


class TestEstate:
    def test_preview_writes_nothing(self, admin_client, estate):
        response = admin_client.post(RECONCILE, {"read_devices": False}, format="json")
        assert response.status_code == 200, response.json()
        result = response.json()["result"]
        assert result["applied"] is False and result["changes"] is None and result["devices"] == []
        assert [entry["pin"] for entry in result["updated"]] == ["2"] and [entry["pin"] for entry in result["added"]] == ["3"] and [entry["pin"] for entry in result["unknown"]] == ["4"]
        assert [entry["pin"] for entry in result["unchanged"]] == ["1"]
        assert codes(result["removed"]) == ["E005"] and codes(result["unconfirmed"]) == ["E009"] and codes(result["unlinked"]) == ["2", "E010"]
        assert result["summary"]["total_device_users"] == 6 and not AuditLog.objects.filter(action__startswith="devices.").exists()

    def test_read_devices_asks_the_agents(self, admin_client, estate):
        result = admin_client.post(RECONCILE, {}, format="json").json()["result"]
        assert result["devices_read_requested"] == 1 and result["devices_not_readable"] == 1
        assert {item["device"]["name"]: item["read_requested"] for item in result["devices"]} == {"A": True, "B": False}

    def test_apply_links_creates_and_deactivates(self, admin_client, estate):
        a, _, people = estate
        result = admin_client.post(RECONCILE, {"read_devices": False, "apply": True, "confirm": True}, format="json").json()["result"]
        assert result["changes"] == {"created": 1, "linked": 1, "deactivated": 1, "skipped": []}
        created = Employee.objects.get(code="3")
        assert created.full_name == "New Person" and a.device_users.get(pin="3").employee == created and a.device_users.get(pin="2").employee == people["2"]
        people["E005"].refresh_from_db()
        assert people["E005"].is_active is False and a.device_users.get(pin="5").employee == people["E005"]  # nothing unlinked or deleted
        assert result["updated"] == [] and result["added"] == [] and result["removed"] == [] and len(result["unchanged"]) == 3

    def test_apply_skips_what_the_caller_may_not_do(self, auth_client, make_user, estate):
        client = auth_client(make_user(grants={"employees": ["view", "edit"], "devices": ["view"]}, scopes={"employees": "all"}))
        result = client.post(RECONCILE, {"read_devices": False, "apply": True, "confirm": True}, format="json").json()["result"]
        assert result["changes"]["linked"] == 1 and result["changes"]["created"] == 0 and result["changes"]["deactivated"] == 0
        assert sorted(item["bucket"] for item in result["changes"]["skipped"]) == ["added", "removed"]

    @pytest.mark.parametrize("scope", ["office", "self"])
    def test_the_estate_spans_every_office_so_it_needs_the_all_scope(self, auth_client, make_user, estate, scope):
        """An office-scoped employees editor saw every office's people here and could deactivate them."""
        a, _, people = estate
        manager = make_user(grants={"employees": ["view", "create", "edit", "archive"], "devices": ["view", "edit"]}, scopes={"employees": scope})
        EmployeeFactory(user=manager, office=OfficeFactory())  # their own office is not the estate's
        client = auth_client(manager)
        for body in ({"read_devices": False}, {"read_devices": False, "apply": True, "confirm": True}):
            response = client.post(RECONCILE, body, format="json")
            assert response.status_code == 403 and response.json()["code"] == "permission_denied", body
        people["E005"].refresh_from_db()
        assert people["E005"].is_active is True and a.device_users.get(pin="2").employee is None
        assert not AuditLog.objects.filter(action__startswith="devices.").exists()

    def test_apply_needs_confirm(self, admin_client, estate):
        response = admin_client.post(RECONCILE, {"apply": True}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "confirmation_required"
