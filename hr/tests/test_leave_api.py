"""hr/leave/ — scoped list, self-service PENDING vs approver APPROVED, approve/reject/cancel with deny_self_action."""

import datetime as dt

import pytest
from freezegun import freeze_time

from audit.models import AuditLog
from hr.models import LeaveRecord
from hr.tests.conftest import events
from hr.tests.factories import EmployeeFactory, LeaveRecordFactory, LeaveTypeFactory, OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/hr/leave/"


def detail(record, suffix=""):
    return f"{URL}{record.uid}/{suffix}"


@pytest.fixture
def leave_type():
    return LeaveTypeFactory(code="CL", name="Casual leave")


def body(leave_type, **extra):
    return {"leave_type": str(leave_type.uid), "date_from": "2026-04-06", "date_to": "2026-04-07", **extra}


class TestPermissionsAndScopes:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {}, format="json").status_code == 401

    def test_grants(self, auth_client, make_user, leave_type):
        record = LeaveRecordFactory(status="PENDING")
        viewer = auth_client(make_user(grants={"leave": ["view"]}, scopes={"leave": "all"}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(record)).status_code == 200
        assert viewer.post(URL, body(leave_type, employee=str(record.employee.uid)), format="json").status_code == 403
        assert viewer.post(detail(record, "approve/"), {}, format="json").status_code == 403
        assert viewer.post(detail(record, "cancel/"), {}, format="json").status_code == 403
        assert auth_client(make_user(grants={"employees": "*"}, scopes={"employees": "all"})).get(URL).status_code == 403

    def test_self_scope(self, auth_client, staff):
        own = LeaveRecordFactory(employee=staff.employee)
        other = LeaveRecordFactory()
        client = auth_client(staff)
        assert [row["uid"] for row in client.get(URL).json()["results"]] == [str(own.uid)]
        assert client.get(detail(other)).status_code == 404

    def test_office_scope(self, auth_client, manager, office):
        colleague = LeaveRecordFactory(employee=EmployeeFactory(office=office))
        LeaveRecordFactory(employee=EmployeeFactory(office=OfficeFactory()))
        rows = auth_client(manager).get(URL).json()["results"]
        assert [row["uid"] for row in rows] == [str(colleague.uid)]

    def test_list_filters_and_n_plus_one(self, hr_client, django_assert_max_num_queries, make_user):
        office = OfficeFactory()
        for index in range(6):
            LeaveRecordFactory(
                employee=EmployeeFactory(office=office, user=make_user()), status="APPROVED", decided_by=make_user(), date_from=dt.date(2026, 3, 1 + index), date_to=dt.date(2026, 3, 1 + index)
            )
        LeaveRecordFactory(status="PENDING", date_from=dt.date(2026, 5, 4), date_to=dt.date(2026, 5, 8))
        with django_assert_max_num_queries(8):
            assert hr_client.get(URL).json()["count"] == 7
        assert hr_client.get(URL, {"status": "PENDING"}).json()["count"] == 1
        assert hr_client.get(URL, {"office": str(office.uid)}).json()["count"] == 6
        assert hr_client.get(URL, {"date_from": "2026-05-05", "date_to": "2026-05-06"}).json()["count"] == 1  # overlap filter
        assert hr_client.get(URL, {"date_from": "2026-03-04"}).json()["count"] == 4


class TestCreate:
    def test_staff_self_service_is_pending(self, auth_client, staff, leave_type):
        response = auth_client(staff).post(URL, body(leave_type, reason="Travel"), format="json")
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["status"] == "PENDING" and data["employee"]["uid"] == str(staff.employee.uid) and data["days"] == 2.0
        assert data["decided_by"] is None and events("hr.attendance_inputs_changed") == []
        assert AuditLog.objects.get(action="hr.leave_created").actor == staff

    def test_hr_filing_for_someone_else_is_approved(self, hr_client, hr_user, leave_type):
        employee = EmployeeFactory()
        data = hr_client.post(URL, body(leave_type, employee=str(employee.uid)), format="json").json()
        assert data["status"] == "APPROVED" and data["decided_by"]["uid"] == str(hr_user.uid) and data["decided_at"]
        assert events("hr.attendance_inputs_changed") == [{"employee_uids": [str(employee.uid)], "date_from": "2026-04-06", "date_to": "2026-04-07", "reason": "leave_approved"}]

    def test_hr_filing_for_themselves_is_pending(self, hr_client, hr_user, leave_type):
        EmployeeFactory(user=hr_user)
        assert hr_client.post(URL, body(leave_type), format="json").json()["status"] == "PENDING"

    def test_a_type_without_approval_is_approved_directly(self, auth_client, staff):
        on_duty = LeaveTypeFactory(code="OD", requires_approval=False)
        assert auth_client(staff).post(URL, body(on_duty), format="json").json()["status"] == "APPROVED"

    def test_staff_cannot_file_for_someone_else(self, auth_client, staff, leave_type):
        response = auth_client(staff).post(URL, body(leave_type, employee=str(EmployeeFactory().uid)), format="json")
        assert response.status_code == 400 and response.json()["code"] == "employee_not_found"

    def test_a_login_without_employee_must_choose_one(self, auth_client, make_user, leave_type):
        user = make_user(grants={"leave": ["view", "create"]}, scopes={"leave": "self"})
        response = auth_client(user).post(URL, body(leave_type), format="json")
        assert response.status_code == 403 and response.json()["code"] == "employee_not_linked"

    def test_inactive_employee(self, hr_client, leave_type):
        response = hr_client.post(URL, body(leave_type, employee=str(EmployeeFactory(is_active=False).uid)), format="json")
        assert response.status_code == 409 and response.json()["code"] == "employee_inactive"

    @pytest.mark.parametrize(
        "extra, field",
        [
            ({"date_to": "2026-04-01"}, "date_to"),
            ({"is_half_day": True}, "is_half_day"),
            ({"date_from": "2026-01-01", "date_to": "2027-01-02"}, "date_to"),
            ({"leave_type": "00000000-0000-0000-0000-000000000000"}, "leave_type"),
        ],
    )
    def test_validation(self, hr_client, leave_type, extra, field):
        response = hr_client.post(URL, {**body(leave_type, employee=str(EmployeeFactory().uid)), **extra}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_half_day(self, hr_client, leave_type):
        data = hr_client.post(URL, body(leave_type, employee=str(EmployeeFactory().uid), date_to="2026-04-06", is_half_day=True), format="json").json()
        assert data["is_half_day"] is True and data["days"] == 0.5

    def test_overlap_with_open_leave_is_refused(self, hr_client, leave_type):
        employee = EmployeeFactory()
        LeaveRecordFactory(employee=employee, status="PENDING", date_from=dt.date(2026, 4, 7), date_to=dt.date(2026, 4, 9))
        response = hr_client.post(URL, body(leave_type, employee=str(employee.uid)), format="json")
        assert response.status_code == 409 and response.json()["code"] == "leave_overlap"
        LeaveRecord.objects.update(status="REJECTED")
        assert hr_client.post(URL, body(leave_type, employee=str(employee.uid)), format="json").status_code == 201


class TestDecisions:
    def test_manager_approves_in_their_office(self, auth_client, manager, office, leave_type):
        record = LeaveRecordFactory(employee=EmployeeFactory(office=office), status="PENDING")
        response = auth_client(manager).post(detail(record, "approve/"), {"expected_version": 1, "note": "ok"}, format="json")
        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["status"] == "APPROVED" and data["decided_by"]["uid"] == str(manager.uid) and data["version"] == 2
        assert events("hr.attendance_inputs_changed")[0]["employee_uids"] == [str(record.employee.uid)]
        assert AuditLog.objects.get(action="hr.leave_approved").note == "ok"

    def test_manager_cannot_reach_another_office(self, auth_client, manager):
        record = LeaveRecordFactory(employee=EmployeeFactory(office=OfficeFactory()), status="PENDING")
        assert auth_client(manager).post(detail(record, "approve/"), {}, format="json").status_code == 404

    def test_nobody_decides_their_own_leave(self, auth_client, manager):
        own = LeaveRecordFactory(employee=manager.employee, status="PENDING")
        for action in ("approve/", "reject/"):
            response = auth_client(manager).post(detail(own, action), {}, format="json")
            assert response.status_code == 403 and response.json()["code"] == "self_action_denied"

    def test_reject_records_the_reason(self, hr_client):
        record = LeaveRecordFactory(status="PENDING")
        data = hr_client.post(detail(record, "reject/"), {"note": "Busy season"}, format="json").json()
        assert data["status"] == "REJECTED" and events("hr.attendance_inputs_changed") == []
        assert AuditLog.objects.get(action="hr.leave_rejected").note == "Busy season"

    def test_only_pending_is_decided(self, hr_client):
        record = LeaveRecordFactory(status="APPROVED")
        response = hr_client.post(detail(record, "approve/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "leave_not_pending"

    def test_approving_into_an_overlap_is_refused(self, hr_client):
        employee = EmployeeFactory()
        LeaveRecordFactory(employee=employee, status="APPROVED", date_from=dt.date(2026, 3, 2), date_to=dt.date(2026, 3, 2))
        record = LeaveRecordFactory(employee=employee, status="PENDING", date_from=dt.date(2026, 3, 2), date_to=dt.date(2026, 3, 3))
        assert hr_client.post(detail(record, "approve/"), {}, format="json").json()["code"] == "leave_overlap"

    def test_stale_version(self, hr_client):
        record = LeaveRecordFactory(status="PENDING", version=2)
        assert hr_client.post(detail(record, "approve/"), {"expected_version": 1}, format="json").json()["code"] == "stale_version"


class TestCancel:
    def test_staff_withdraws_pending_leave(self, auth_client, staff):
        record = LeaveRecordFactory(employee=staff.employee, status="PENDING")
        data = auth_client(staff).post(detail(record, "cancel/"), {}, format="json").json()
        assert data["status"] == "CANCELLED" and events("hr.attendance_inputs_changed") == []

    def test_staff_cancels_approved_leave_only_before_it_starts(self, auth_client, staff):
        record = LeaveRecordFactory(employee=staff.employee, status="APPROVED", date_from=dt.date(2026, 3, 10), date_to=dt.date(2026, 3, 11))
        with freeze_time("2026-03-10T03:00:00Z"):
            response = auth_client(staff).post(detail(record, "cancel/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "leave_already_started"
        with freeze_time("2026-03-09T03:00:00Z"):
            data = auth_client(staff).post(detail(record, "cancel/"), {}, format="json").json()
        assert data["status"] == "CANCELLED"
        assert events("hr.attendance_inputs_changed")[0]["reason"] == "leave_cancelled"

    def test_staff_cannot_cancel_someone_else_s(self, auth_client, make_user, office):
        # a self-scoped role cannot even see it; an all-scoped creator without approve is refused
        creator = make_user(grants={"leave": ["view", "create"]}, scopes={"leave": "all"})
        record = LeaveRecordFactory(status="PENDING")
        response = auth_client(creator).post(detail(record, "cancel/"), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "permission_denied"

    def test_hr_cancels_anyone_s_approved_leave(self, hr_client):
        record = LeaveRecordFactory(status="APPROVED", date_from=dt.date(2020, 1, 1), date_to=dt.date(2020, 1, 2))
        assert hr_client.post(detail(record, "cancel/"), {"note": "Came to work"}, format="json").json()["status"] == "CANCELLED"

    def test_closed_leave_cannot_be_cancelled(self, hr_client):
        record = LeaveRecordFactory(status="REJECTED")
        response = hr_client.post(detail(record, "cancel/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "leave_not_cancellable"
