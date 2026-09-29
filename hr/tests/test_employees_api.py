"""hr/employees/ — CRUD, scopes (all/office/self), lifecycle + hr.employee_deactivated, dependencies, device registries."""

import datetime as dt

import pytest

from accounts.models import UserSession
from audit.models import AuditLog
from hr import registries
from hr.models import Employee
from hr.tests.conftest import events
from hr.tests.factories import EmployeeFactory, LeaveRecordFactory, OfficeFactory, ShiftFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/hr/employees/"


def detail(employee, suffix=""):
    return f"{URL}{employee.uid}/{suffix}"


class TestPermissionsAndScopes:
    def test_anonymous_is_401(self, api_client):
        employee = EmployeeFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(detail(employee, "deactivate/"), {}, format="json").status_code == 401

    def test_each_action_needs_its_grant(self, auth_client, make_user):
        employee = EmployeeFactory()
        viewer = auth_client(make_user(grants={"employees": ["view"]}, scopes={"employees": "all"}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(employee)).status_code == 200
        assert viewer.get(detail(employee, "dependencies/")).status_code == 200
        assert viewer.get(detail(employee, "device-mappings/")).status_code == 200
        assert viewer.post(URL, {"code": "X", "full_name": "X"}, format="json").status_code == 403
        assert viewer.patch(detail(employee), {"full_name": "x"}, format="json").status_code == 403
        assert viewer.post(detail(employee, "deactivate/"), {}, format="json").status_code == 403
        assert viewer.post(detail(employee, "link-user/"), {"email": "a@example.com"}, format="json").status_code == 403
        assert viewer.delete(detail(employee)).status_code == 403
        editor = auth_client(make_user(grants={"employees": ["view", "edit"]}, scopes={"employees": "all"}))
        assert editor.patch(detail(employee), {"designation": "Lead"}, format="json").status_code == 200
        assert editor.post(detail(employee, "deactivate/"), {}, format="json").status_code == 403
        assert auth_client(make_user(grants={"hr_setup": "*"})).get(URL).status_code == 403

    def test_office_scope_sees_only_the_manager_s_office(self, auth_client, manager, office):
        colleague = EmployeeFactory(office=office)
        other = EmployeeFactory(office=OfficeFactory())
        client = auth_client(manager)
        codes = {row["code"] for row in client.get(URL).json()["results"]}
        assert colleague.code in codes and other.code not in codes and len(codes) == 2
        assert client.get(detail(other)).status_code == 404

    def test_self_scope_sees_only_the_own_record(self, auth_client, make_user):
        user = make_user(grants={"employees": ["view"]}, scopes={"employees": "self"})
        own = EmployeeFactory(user=user)
        EmployeeFactory.create_batch(2)
        client = auth_client(user)
        assert [row["code"] for row in client.get(URL).json()["results"]] == [own.code]

    def test_narrow_scope_without_a_linked_employee_sees_nothing(self, auth_client, make_user):
        EmployeeFactory.create_batch(2)
        user = make_user(grants={"employees": ["view"]}, scopes={"employees": "office"})
        assert auth_client(user).get(URL).json()["count"] == 0


class TestCreateListUpdate:
    def test_create_normalises_and_audits(self, hr_client, hr_user):
        office, shift = OfficeFactory(), ShiftFactory()
        payload = {
            "code": " E100 ",
            "full_name": "Asha Menon",
            "office": str(office.uid),
            "shift": str(shift.uid),
            "department": "Accounts",
            "designation": "Accountant",
            "email": "asha@example.com",
            "phone_e164": "98470 12345",
            "joined_on": "2024-04-01",
            "identity_method": "FACE",
        }
        response = hr_client.post(URL, payload, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["code"] == "E100" and body["phone_e164"] == "+919847012345" and body["office"]["uid"] == str(office.uid)
        assert body["shift"]["uid"] == str(shift.uid) and body["effective_shift"]["uid"] == str(shift.uid)
        assert body["is_active"] is True and body["user"] is None and body["photo"] is None and body["identity_method"] == "FACE"
        assert AuditLog.objects.get(action="hr.employee_created").actor == hr_user

    @pytest.mark.parametrize(
        "payload, field",
        [
            ({"phone_e164": "12345"}, "phone_e164"),
            ({"email": "not-an-email"}, "email"),
            ({"identity_method": "IRIS"}, "identity_method"),
            ({"joined_on": "2025-01-10", "left_on": "2025-01-01"}, "left_on"),
            ({"code": ""}, "code"),
        ],
    )
    def test_validation(self, hr_client, payload, field):
        response = hr_client.post(URL, {"code": "E1", "full_name": "X", **payload}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_codes_are_unique_case_insensitive(self, hr_client):
        EmployeeFactory(code="E001")
        response = hr_client.post(URL, {"code": "e001", "full_name": "Twin"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "employee_code_taken"

    def test_effective_shift_falls_back_to_the_office_default(self, hr_client):
        default = ShiftFactory(code="GEN")
        employee = EmployeeFactory(office=OfficeFactory(default_shift=default), shift=None)
        assert hr_client.get(detail(employee)).json()["effective_shift"]["code"] == "GEN"

    def test_list_defaults_to_active_and_avoids_n_plus_one(self, hr_client, django_assert_max_num_queries):
        default = ShiftFactory()
        for index in range(6):
            EmployeeFactory(office=OfficeFactory(default_shift=default), shift=ShiftFactory() if index % 2 else None)
        EmployeeFactory(is_active=False, full_name="Zed Gone")
        with django_assert_max_num_queries(8):
            body = hr_client.get(URL).json()
        assert body["count"] == 6
        assert hr_client.get(URL, {"include_inactive": "true"}).json()["count"] == 7
        assert [row["full_name"] for row in hr_client.get(URL, {"is_active": "false"}).json()["results"]] == ["Zed Gone"]
        assert hr_client.get(URL, {"filter[is_active]": "false"}).json()["count"] == 1
        assert hr_client.get(URL, {"search": "zed", "include_inactive": "true"}).json()["count"] == 1

    def test_filters(self, hr_client, make_user):
        office = OfficeFactory()
        EmployeeFactory(office=office, identity_method="CARD", user=make_user())
        EmployeeFactory(office=office)
        EmployeeFactory()
        assert hr_client.get(URL, {"office": str(office.uid)}).json()["count"] == 2
        assert hr_client.get(URL, {"has_login": "true"}).json()["count"] == 1
        assert hr_client.get(URL, {"has_login": "false"}).json()["count"] == 2
        assert hr_client.get(URL, {"identity_method": "CARD"}).json()["count"] == 1

    def test_update_recomputes_on_office_or_shift_change(self, hr_client):
        employee = EmployeeFactory()
        assert hr_client.patch(detail(employee), {"designation": "Lead"}, format="json").status_code == 200
        assert events("hr.attendance_inputs_changed") == []
        response = hr_client.patch(detail(employee), {"shift": str(ShiftFactory().uid), "expected_version": 2}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 3
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["employee_uids"] == [str(employee.uid)] and payload["reason"] == "employee_updated"

    def test_joined_on_change_recomputes_from_the_earlier_date(self, hr_client):
        employee = EmployeeFactory(joined_on=dt.date(2020, 1, 1))
        hr_client.patch(detail(employee), {"joined_on": "2019-06-01"}, format="json")
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["date_from"] == "2019-06-01"

    def test_stale_version(self, hr_client):
        employee = EmployeeFactory(version=4)
        response = hr_client.patch(detail(employee), {"full_name": "x", "expected_version": 3}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert hr_client.post(detail(employee, "deactivate/"), {"expected_version": 3}, format="json").status_code == 409


class TestLifecycle:
    def test_deactivate_emits_the_accounts_contract_and_the_login_is_retired(self, hr_client, make_user, drain_outbox, auth_client):
        login = make_user()
        auth_client(login)  # an open session
        employee = EmployeeFactory(user=login)
        response = hr_client.post(detail(employee, "deactivate/"), {"left_on": "2026-03-31", "note": "Resigned"}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["is_active"] is False and response.json()["left_on"] == "2026-03-31"
        assert events("hr.employee_deactivated") == [{"employee_uid": str(employee.uid), "user_uid": str(login.uid)}]
        drain_outbox()
        login.refresh_from_db()
        assert login.is_active is False and not UserSession.objects.filter(user=login, revoked_at__isnull=True).exists()
        assert AuditLog.objects.get(action="hr.employee_deactivated").note == "Resigned"

    def test_deactivating_an_employee_without_login_sends_a_null_user(self, hr_client):
        employee = EmployeeFactory()
        hr_client.post(detail(employee, "deactivate/"), {}, format="json")
        assert events("hr.employee_deactivated") == [{"employee_uid": str(employee.uid), "user_uid": None}]

    def test_deactivate_is_idempotent(self, hr_client, make_user):
        employee = EmployeeFactory(is_active=False)
        assert hr_client.post(detail(employee, "deactivate/"), {}, format="json").json()["version"] == 1
        retired = EmployeeFactory(is_active=False, user=make_user(is_active=False))
        assert hr_client.post(detail(retired, "deactivate/"), {}, format="json").json()["version"] == 1
        assert events("hr.employee_deactivated") == []

    def test_deactivating_again_retires_a_login_that_is_still_active(self, hr_client, make_user, drain_outbox):
        # eSSL's deactivate kept the login (the import reports it: "deactivate it again to retire the login")
        login = make_user()
        employee = EmployeeFactory(is_active=False, user=login)
        response = hr_client.post(detail(employee, "deactivate/"), {"note": "Left in 2025"}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["is_active"] is False and response.json()["version"] == 1
        assert events("hr.employee_deactivated") == [{"employee_uid": str(employee.uid), "user_uid": str(login.uid)}]
        assert AuditLog.objects.get(action="hr.employee_deactivated").note == "Left in 2025"
        drain_outbox()
        login.refresh_from_db()
        assert login.is_active is False
        assert hr_client.post(detail(employee, "deactivate/"), {}, format="json").status_code == 200
        assert len(events("hr.employee_deactivated")) == 1  # nothing left to retire

    def test_nobody_deactivates_their_own_record(self, auth_client, hr_user):
        own = EmployeeFactory(user=hr_user)
        response = auth_client(hr_user).post(detail(own, "deactivate/"), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "self_action_denied"

    def test_nobody_deactivates_a_login_they_could_not_manage(self, hr_client, make_user):
        from accounts.tests.factories import super_admin_role

        boss = EmployeeFactory(user=make_user(role=super_admin_role()))
        response = hr_client.post(detail(boss, "deactivate/"), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "super_admin_required"
        powerful = EmployeeFactory(user=make_user(grants={"pricing": "*"}))
        assert hr_client.post(detail(powerful, "deactivate/"), {}, format="json").json()["code"] == "user_exceeds_own_grants"

    def test_activate_clears_left_on_and_keeps_the_login_as_is(self, hr_client, make_user):
        login = make_user(is_active=False)
        employee = EmployeeFactory(is_active=False, left_on=dt.date(2026, 1, 31), user=login)
        body = hr_client.post(detail(employee, "activate/"), {"expected_version": 1}, format="json").json()
        assert body["is_active"] is True and body["left_on"] is None and body["user"]["is_active"] is False
        assert events("hr.attendance_inputs_changed")[0]["reason"] == "employee_activated"


class TestDependenciesAndDelete:
    def test_dependencies_count_history_from_every_package(self, hr_client, make_user):
        employee = EmployeeFactory(user=make_user())
        LeaveRecordFactory(employee=employee)
        registries.employee_dependencies.register("attendance")(lambda row: {"attendance_days": 12, "raw_punches": 30})
        try:
            body = hr_client.get(detail(employee, "dependencies/")).json()
        finally:
            registries.employee_dependencies.unregister("attendance")
        assert body["counts"] == {"leave_records": 1, "attendance_days": 12, "raw_punches": 30, "device_mappings": 0}  # device_mappings: the installed devices package
        assert body["has_login"] is True and body["can_delete"] is False and body["employee"]["code"] == employee.code

    def test_delete_is_refused_with_history(self, hr_client):
        employee = EmployeeFactory()
        LeaveRecordFactory(employee=employee, status="CANCELLED").soft_delete()
        response = hr_client.delete(detail(employee))
        assert response.status_code == 409 and response.json()["code"] == "employee_has_history" and response.json()["errors"] == {"leave_records": ["1"]}

    def test_delete_without_history_retires_the_login(self, hr_client, make_user):
        login = make_user()
        employee = EmployeeFactory(user=login)
        assert hr_client.get(detail(employee, "dependencies/")).json()["can_delete"] is True
        assert hr_client.delete(detail(employee)).status_code == 204
        row = Employee.all_objects.get(pk=employee.pk)
        assert row.deleted_at is not None and row.user_id is None
        assert events("hr.employee_deactivated") == [{"employee_uid": str(employee.uid), "user_uid": str(login.uid)}]
        assert hr_client.get(detail(employee)).status_code == 404

    def test_nobody_deletes_their_own_record(self, auth_client, hr_user):
        own = EmployeeFactory(user=hr_user)
        assert auth_client(hr_user).delete(detail(own)).json()["code"] == "self_action_denied"


class TestDeviceRegistries:
    def test_device_mappings_are_empty_until_the_devices_package_provides_them(self, hr_client, monkeypatch):
        monkeypatch.setattr(registries.device_mappings, "_fn", None)  # as before the devices package installs its provider
        employee = EmployeeFactory()
        body = hr_client.get(detail(employee, "device-mappings/")).json()
        assert body == {"employee": body["employee"], "available": False, "mappings": [], "details": {}}
        registries.device_mappings.set(lambda row: {"mappings": [{"device": "MARS-01", "pin": "7"}], "active_on_devices": 1})
        try:
            body = hr_client.get(detail(employee, "device-mappings/")).json()
        finally:
            registries.device_mappings.set(None)
        assert body["available"] is True and body["mappings"] == [{"device": "MARS-01", "pin": "7"}] and body["details"] == {"active_on_devices": 1}

    def test_reconcile_devices(self, hr_client, hr_user, monkeypatch):
        monkeypatch.setattr(registries.device_reconciler, "_fn", None)  # as before the devices package installs its provider
        url = f"{URL}reconcile-devices/"
        response = hr_client.post(url, {}, format="json")
        assert response.status_code == 503 and response.json()["code"] == "devices_unavailable"
        response = hr_client.post(url, {"apply": True}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "confirmation_required"
        calls = []
        registries.device_reconciler.set(lambda **kwargs: calls.append(kwargs) or {"added": [], "removed": []})
        try:
            response = hr_client.post(url, {"read_devices": False, "apply": True, "confirm": True}, format="json")
        finally:
            registries.device_reconciler.set(None)
        assert response.status_code == 200 and response.json() == {"result": {"added": [], "removed": []}}
        assert calls == [{"user": hr_user, "read_devices": False, "apply": True}]

    def test_reconcile_needs_edit(self, auth_client, make_user):
        client = auth_client(make_user(grants={"employees": ["view"]}, scopes={"employees": "all"}))
        assert client.post(f"{URL}reconcile-devices/", {}, format="json").status_code == 403
