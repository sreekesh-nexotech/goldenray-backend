"""devices/device-users/ — per-device rows (A1): link this row only, auto-link, resolve, map-pin, unmapped, states."""

from datetime import datetime

import pytest

from audit.models import AuditLog
from devices.models import DeviceUser
from devices.services import ingest, roster
from devices.tests.conftest import events
from devices.tests.factories import DeviceFactory, DeviceUserFactory, SyncLogFactory, users_log
from hr.models import Employee
from hr.services.recompute import EVENT as RECOMPUTE
from hr.tests.factories import EmployeeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/devices/device-users/"


def detail(row, suffix=""):
    return f"{URL}{row.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        row = DeviceUserFactory()
        for method, path in [("get", URL), ("get", detail(row)), ("post", detail(row, "link/")), ("post", f"{URL}auto-link/"), ("post", f"{URL}map-pin/"), ("get", f"{URL}unmapped/")]:
            assert getattr(api_client, method)(path).status_code == 401

    def test_linking_needs_edit(self, hr_client, viewer_client, auth_client, make_user):
        row = DeviceUserFactory()
        employee = EmployeeFactory()
        assert viewer_client.get(URL).status_code == 200 and viewer_client.get(detail(row)).status_code == 200
        for client in (hr_client, viewer_client):
            assert client.post(detail(row, "link/"), {"employee_uid": str(employee.uid)}, format="json").status_code == 403
            assert client.post(f"{URL}auto-link/").status_code == 403
            assert client.post(f"{URL}map-pin/", {"device_uid": str(row.device.uid), "pin": "9", "employee_uid": str(employee.uid)}, format="json").status_code == 403
        assert auth_client(make_user(grants={"employees": ["view"]})).get(URL).status_code == 403


class TestList:
    def test_states_and_filters_without_n_plus_one(self, admin_client, django_assert_max_num_queries):
        device, other = DeviceFactory(), DeviceFactory()
        gone = EmployeeFactory(is_active=False)
        active = DeviceUserFactory(device=device, pin="1", employee=EmployeeFactory())
        leaver = DeviceUserFactory(device=device, pin="2", employee=gone)
        DeviceUserFactory(device=device, pin="3")
        DeviceUserFactory(device=other, pin="1")
        users_log(device, ["1", "2"])
        with django_assert_max_num_queries(8):
            rows = admin_client.get(URL, {"device": str(device.uid)}).json()["results"]
        states = {row["pin"]: (row["device_state"], row["software_state"], row["is_active_user"], row["needs_device_removal"]) for row in rows}
        assert states == {
            "1": (roster.ACTIVE_ON_DEVICE, roster.SOFTWARE_ACTIVE, True, False),
            "2": (roster.ACTIVE_ON_DEVICE, roster.SOFTWARE_DEACTIVATED, False, True),
            "3": (roster.MISSING_FROM_DEVICE, roster.SOFTWARE_UNLINKED, False, False),
        }
        assert {row["pin"] for row in admin_client.get(URL, {"device": str(device.uid), "active_only": "true"}).json()["results"]} == {"1"}
        assert {row["uid"] for row in admin_client.get(URL, {"device": str(device.uid), "software_state": "DEACTIVATED"}).json()["results"]} == {str(leaver.uid)}
        assert {row["pin"] for row in admin_client.get(URL, {"device_state": "MISSING_FROM_DEVICE"}).json()["results"]} == {"3"}
        pending = admin_client.get(URL, {"device": str(other.uid)}).json()["results"][0]
        assert pending["device_state"] == roster.PENDING_SYNC and pending["sync_state"] == roster.SYNC_NEVER_RUN
        assert {row["uid"] for row in admin_client.get(URL, {"linked": "true"}).json()["results"]} == {str(active.uid), str(leaver.uid)}
        assert admin_client.get(URL, {"device_state": "NOPE"}).status_code == 400

    def test_failed_read_and_inactive_device(self, admin_client):
        device = DeviceFactory()
        row = DeviceUserFactory(device=device)
        SyncLogFactory(device=device, status="FAILED", error_message="timed out")
        body = admin_client.get(detail(row)).json()
        assert body["device_state"] == roster.SYNC_FAILED and body["sync_state"] == roster.SYNC_LAST_FAILED and body["sync_error"] == "timed out"
        device.is_active = False
        device.save(update_fields=["is_active"])
        assert admin_client.get(detail(row)).json()["device_state"] == roster.DEVICE_INACTIVE


class TestLink:
    def test_links_this_row_only_and_recomputes(self, admin_client):
        employee = EmployeeFactory()
        row = DeviceUserFactory(pin="7")
        elsewhere = DeviceUserFactory(pin="7")
        response = admin_client.post(detail(row, "link/"), {"employee_uid": str(employee.uid), "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["employee"]["uid"] == str(employee.uid) and response.json()["version"] == 2
        elsewhere.refresh_from_db()
        assert elsewhere.employee_id is None
        [event] = events(RECOMPUTE)
        assert event["employee_uids"] == [str(employee.uid)] and event["reason"] == "device_link_changed"
        assert AuditLog.objects.filter(action="devices.device_user_linked").count() == 1

    def test_unlink_relink_and_stale(self, admin_client):
        before, after = EmployeeFactory(), EmployeeFactory()
        row = DeviceUserFactory(employee=before)
        assert admin_client.post(detail(row, "link/"), {"employee_uid": None}, format="json").json()["employee"] is None
        assert admin_client.post(detail(row, "link/"), {"employee_uid": str(after.uid), "expected_version": 1}, format="json").json()["code"] == "stale_version"
        admin_client.post(detail(row, "link/"), {"employee_uid": str(after.uid)}, format="json")
        assert sorted(events(RECOMPUTE)[-1]["employee_uids"]) == [str(after.uid)] and events(RECOMPUTE)[0]["employee_uids"] == [str(before.uid)]
        assert AuditLog.objects.filter(action="devices.device_user_unlinked").exists()

    def test_validation(self, admin_client):
        row = DeviceUserFactory()
        response = admin_client.post(detail(row, "link/"), {"employee_uid": "00000000-0000-0000-0000-000000000000"}, format="json")
        assert response.status_code == 400 and "employee_uid" in response.json()["errors"]
        assert admin_client.post(f"{URL}not-a-uid/link/", {}, format="json").status_code == 404


class TestAutoLink:
    def test_pin_equals_code_never_overwrites(self, admin_client):
        device = DeviceFactory()
        EmployeeFactory(code="e001")
        taken = EmployeeFactory(code="E002")
        mine = EmployeeFactory(code="E003")
        DeviceUserFactory(device=device, pin="E001")
        DeviceUserFactory(device=device, pin="E002", employee=mine)
        DeviceUserFactory(device=device, pin="X9")
        DeviceUserFactory(device=DeviceFactory(is_active=False), pin="E001")
        response = admin_client.post(f"{URL}auto-link/", QUERY_STRING=f"device={device.uid}")
        assert response.json() == {"linked": 1, "still_unlinked": 1, "unlinked_pins": ["X9"]}
        assert DeviceUser.objects.get(device=device, pin="E002").employee == mine and taken.device_users.count() == 0
        assert admin_client.post(f"{URL}auto-link/").json()["linked"] == 0  # the inactive device's row stays unlinked

    def test_unknown_device(self, admin_client):
        assert admin_client.post(f"{URL}auto-link/?device=00000000-0000-0000-0000-000000000000").status_code == 400


class TestResolve:
    def test_confirmation_required(self, admin_client):
        row = DeviceUserFactory()
        response = admin_client.post(detail(row, "resolve/"), {"action": "LINK_EXISTING", "employee_uid": str(EmployeeFactory().uid)}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "confirmation_required"

    def test_link_existing_and_conflict(self, admin_client):
        owner, other = EmployeeFactory(), EmployeeFactory()
        row = DeviceUserFactory()
        assert admin_client.post(detail(row, "resolve/"), {"action": "LINK_EXISTING", "confirm": True}, format="json").json()["errors"] == {"employee_uid": ["Required to link."]}
        assert admin_client.post(detail(row, "resolve/"), {"action": "LINK_EXISTING", "confirm": True, "employee_uid": str(owner.uid)}, format="json").status_code == 200
        clash = admin_client.post(detail(row, "resolve/"), {"action": "LINK_EXISTING", "confirm": True, "employee_uid": str(other.uid)}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "device_user_already_linked"
        create = admin_client.post(detail(row, "resolve/"), {"action": "CREATE_EMPLOYEE", "confirm": True}, format="json")
        assert create.status_code == 409 and create.json()["code"] == "device_user_already_linked"

    def test_create_employee_defaults_to_the_terminal(self, admin_client):
        row = DeviceUserFactory(pin="44", name="Asha K")
        response = admin_client.post(detail(row, "resolve/"), {"action": "CREATE_EMPLOYEE", "confirm": True}, format="json")
        assert response.status_code == 200, response.json()
        employee = Employee.objects.get(code="44")
        assert employee.full_name == "Asha K" and employee.office_id == row.device.office_id and response.json()["employee"]["uid"] == str(employee.uid)

    def test_create_employee_needs_employees_create_and_a_name(self, auth_client, make_user, admin_client):
        editor = auth_client(make_user(grants={"devices": ["view", "edit"], "employees": ["view"]}, scopes={"employees": "all"}))
        row = DeviceUserFactory(name="")
        assert editor.post(detail(row, "resolve/"), {"action": "CREATE_EMPLOYEE", "confirm": True, "full_name": "X"}, format="json").status_code == 403
        nameless = admin_client.post(detail(row, "resolve/"), {"action": "CREATE_EMPLOYEE", "confirm": True}, format="json")
        assert nameless.status_code == 400 and "full_name" in nameless.json()["errors"]
        assert admin_client.post(detail(row, "resolve/"), {"action": "IDENTIFY", "confirm": True}, format="json").status_code == 400


class TestMapPin:
    def test_creates_the_row_for_a_pin_seen_only_in_punches(self, admin_client):
        device, employee = DeviceFactory(), EmployeeFactory()
        payload = {"device_uid": str(device.uid), "pin": " 55 ", "employee_uid": str(employee.uid)}
        body = admin_client.post(f"{URL}map-pin/", payload, format="json").json()
        assert body["created"] is True and body["device_user"]["pin"] == "55" and body["device_user"]["employee"]["uid"] == str(employee.uid) and body["note"]
        again = admin_client.post(f"{URL}map-pin/", payload, format="json").json()
        assert again["created"] is False and DeviceUser.objects.filter(device=device, pin="55").count() == 1

    def test_validation(self, admin_client):
        device, employee = DeviceFactory(), EmployeeFactory()
        response = admin_client.post(f"{URL}map-pin/", {"device_uid": str(device.uid), "pin": " ", "employee_uid": str(employee.uid)}, format="json")
        assert response.status_code == 400 and "pin" in response.json()["errors"]
        assert set(admin_client.post(f"{URL}map-pin/", {}, format="json").json()["errors"]) == {"device_uid", "pin", "employee_uid"}


class TestUnmapped:
    def test_pins_with_punches_but_no_link(self, admin_client, sink):
        device = DeviceFactory()
        suggested = EmployeeFactory(code="3")
        DeviceUserFactory(device=device, pin="1", employee=EmployeeFactory())
        DeviceUserFactory(device=device, pin="2", name="Ravi")
        records = [{"pin": pin, "device_time": datetime(2026, 9, 1, 9, minute)} for minute, pin in enumerate(["1", "2", "2", "3"])]
        ingest.ingest(device, records, source=ingest.AGENT_PUSH)
        rows = admin_client.get(f"{URL}unmapped/", {"device": str(device.uid)}).json()["results"]
        assert [(row["pin"], row["punch_count"], row["has_device_user_row"]) for row in rows] == [("2", 2, True), ("3", 1, False)]
        assert rows[0]["name"] == "Ravi" and rows[1]["suggested_employee"]["uid"] == str(suggested.uid)
        assert admin_client.get(f"{URL}unmapped/").status_code == 400

    def test_empty_without_a_punch_store(self, admin_client):
        assert admin_client.get(f"{URL}unmapped/", {"device": str(DeviceFactory().uid)}).json()["results"] == []
