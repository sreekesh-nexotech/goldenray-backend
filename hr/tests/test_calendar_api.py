"""hr/holidays/, hr/leave-types/, hr/attendance-rules/ (module hr_setup)."""

import datetime as dt

import pytest
from django.db import IntegrityError

from audit.models import AuditLog
from hr.models import AttendanceRule, Holiday, LeaveType
from hr.tests.conftest import events
from hr.tests.factories import AttendanceRuleFactory, EmployeeFactory, HolidayFactory, LeaveRecordFactory, LeaveTypeFactory, OfficeFactory, ShiftFactory

pytestmark = pytest.mark.django_db
HOLIDAYS = "/api/v1/hr/holidays/"
TYPES = "/api/v1/hr/leave-types/"
RULES = "/api/v1/hr/attendance-rules/"


@pytest.mark.parametrize("url", [HOLIDAYS, RULES])
def test_permissions(url, api_client, auth_client, make_user):
    assert api_client.get(url).status_code == 401
    viewer = auth_client(make_user(grants={"hr_setup": ["view"]}))
    assert viewer.get(url).status_code == 200
    assert viewer.post(url, {"name": "x"}, format="json").status_code == 403
    assert auth_client(make_user(grants={"leave": "*"}, scopes={"leave": "all"})).get(url).status_code == 403


def test_leave_types_are_read_by_whoever_files_or_decides_leave(api_client, auth_client, make_user, staff, manager):
    """Self-service leave (PLAN §3.2 Staff: leave view/create) needs the leave-type uids; the Office Manager decides
    leave of those types. Neither holds hr_setup: reading is ``leave.view``, writing stays ``hr_setup.edit``."""
    leave_type = LeaveTypeFactory(code="CL", name="Casual leave")
    assert api_client.get(TYPES).status_code == 401
    for user in (staff, manager):
        client = auth_client(user)
        response = client.get(TYPES)
        assert response.status_code == 200, response.json()
        assert [row["code"] for row in response.json()["results"]] == ["CL"]
        assert client.get(f"{TYPES}{leave_type.uid}/").json()["name"] == "Casual leave"
        assert client.post(TYPES, {"code": "X", "name": "X"}, format="json").status_code == 403
        assert client.patch(f"{TYPES}{leave_type.uid}/", {"name": "x"}, format="json").status_code == 403
        assert client.delete(f"{TYPES}{leave_type.uid}/").status_code == 403
    editor = auth_client(make_user(grants={"hr_setup": ["view", "edit"], "leave": ["view"]}, scopes={"leave": "all"}))
    assert editor.get(TYPES).status_code == 200
    assert editor.post(TYPES, {"code": "SL", "name": "Sick leave"}, format="json").status_code == 201
    assert auth_client(make_user(grants={"employees": "*"}, scopes={"employees": "all"})).get(TYPES).status_code == 403


class TestHolidays:
    def test_create_office_and_global_and_recompute(self, hr_client, hr_user):
        office = OfficeFactory()
        response = hr_client.post(HOLIDAYS, {"office": str(office.uid), "date": "2026-03-10", "name": "Foundation Day", "notes": "Half the team"}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["office"]["uid"] == str(office.uid) and body["is_global"] is False and body["notes"] == "Half the team"
        body = hr_client.post(HOLIDAYS, {"date": "2026-01-26", "name": "Republic Day"}, format="json").json()
        assert body["office"] is None and body["is_global"] is True
        assert events("hr.attendance_inputs_changed") == [
            {"office_uid": str(office.uid), "date_from": "2026-03-10", "date_to": "2026-03-10", "reason": "holiday_changed"},
            {"office_uid": None, "date_from": "2026-01-26", "date_to": "2026-01-26", "reason": "holiday_changed"},
        ]
        assert AuditLog.objects.filter(action="hr.holiday_created", actor=hr_user).count() == 2

    def test_both_partial_uniques(self, hr_client):
        office = OfficeFactory()
        HolidayFactory(office=office, date=dt.date(2026, 3, 10))
        HolidayFactory(office=None, date=dt.date(2026, 1, 26))
        clash = hr_client.post(HOLIDAYS, {"office": str(office.uid), "date": "2026-03-10", "name": "x"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "holiday_exists"
        clash = hr_client.post(HOLIDAYS, {"date": "2026-01-26", "name": "x"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "holiday_exists"
        # a global and an office holiday may share a date; a soft-deleted one frees the slot
        assert hr_client.post(HOLIDAYS, {"office": str(office.uid), "date": "2026-01-26", "name": "x"}, format="json").status_code == 201
        Holiday.objects.get(office=None, date=dt.date(2026, 1, 26)).soft_delete()
        assert hr_client.post(HOLIDAYS, {"date": "2026-01-26", "name": "again"}, format="json").status_code == 201

    def test_update_to_a_taken_date_is_refused_too(self, hr_client):
        HolidayFactory(office=None, date=dt.date(2026, 1, 26))
        other = HolidayFactory(office=None, date=dt.date(2026, 10, 2))
        response = hr_client.patch(f"{HOLIDAYS}{other.uid}/", {"date": "2026-01-26"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "holiday_exists"

    def test_db_enforces_the_global_unique(self):
        HolidayFactory(office=None, date=dt.date(2026, 1, 26))
        with pytest.raises(IntegrityError):
            HolidayFactory(office=None, date=dt.date(2026, 1, 26))

    def test_update_recomputes_old_and_new_scope(self, hr_client):
        first, second = OfficeFactory(), OfficeFactory()
        holiday = HolidayFactory(office=first, date=dt.date(2026, 3, 10))
        response = hr_client.patch(f"{HOLIDAYS}{holiday.uid}/", {"office": str(second.uid), "date": "2026-03-12", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        assert [(row["office_uid"], row["date_from"]) for row in events("hr.attendance_inputs_changed")] == [(str(first.uid), "2026-03-10"), (str(second.uid), "2026-03-12")]
        hr_client.patch(f"{HOLIDAYS}{holiday.uid}/", {"name": "Renamed"}, format="json")
        assert len(events("hr.attendance_inputs_changed")) == 2

    def test_filters_list_and_n_plus_one(self, hr_client, django_assert_max_num_queries):
        office = OfficeFactory()
        HolidayFactory(office=None, date=dt.date(2026, 1, 26))
        HolidayFactory(office=office, date=dt.date(2026, 3, 10))
        HolidayFactory(office=OfficeFactory(), date=dt.date(2026, 3, 11))
        HolidayFactory(office=None, date=dt.date(2025, 12, 25), is_active=False)
        with django_assert_max_num_queries(6):
            assert hr_client.get(HOLIDAYS).json()["count"] == 4
        assert hr_client.get(HOLIDAYS, {"office": str(office.uid)}).json()["count"] == 3  # its own + the global ones
        assert hr_client.get(HOLIDAYS, {"year": 2026}).json()["count"] == 3
        assert hr_client.get(HOLIDAYS, {"is_global": "true"}).json()["count"] == 2
        assert hr_client.get(HOLIDAYS, {"is_active": "false"}).json()["count"] == 1
        assert hr_client.get(HOLIDAYS, {"date_from": "2026-03-01", "date_to": "2026-03-31"}).json()["count"] == 2

    def test_soft_delete_and_stale_version(self, hr_client):
        holiday = HolidayFactory(version=2)
        assert hr_client.delete(f"{HOLIDAYS}{holiday.uid}/?expected_version=1").json()["code"] == "stale_version"
        assert hr_client.delete(f"{HOLIDAYS}{holiday.uid}/").status_code == 204
        assert Holiday.all_objects.get(pk=holiday.pk).deleted_at is not None
        assert len(events("hr.attendance_inputs_changed")) == 1

    def test_validation(self, hr_client):
        response = hr_client.post(HOLIDAYS, {"date": "someday", "name": ""}, format="json")
        assert response.status_code == 400 and {"date", "name"} <= set(response.json()["errors"])


class TestLeaveTypes:
    def test_crud(self, hr_client):
        response = hr_client.post(TYPES, {"code": "SICK", "name": "Sick leave", "paid": True, "requires_approval": False}, format="json")
        assert response.status_code == 201, response.json()
        uid = response.json()["uid"]
        assert response.json()["record_count"] == 0 and response.json()["requires_approval"] is False
        clash = hr_client.post(TYPES, {"code": "sick", "name": "Other"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "leave_type_code_taken"
        response = hr_client.patch(f"{TYPES}{uid}/", {"paid": False, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["paid"] is False and response.json()["version"] == 2
        assert hr_client.patch(f"{TYPES}{uid}/", {"paid": True, "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert hr_client.delete(f"{TYPES}{uid}/").status_code == 204
        assert LeaveType.all_objects.get(uid=uid).deleted_at is not None

    def test_in_use_types_cannot_be_deleted_and_counts_are_annotated(self, hr_client, django_assert_max_num_queries):
        used = LeaveTypeFactory()
        LeaveRecordFactory.create_batch(2, leave_type=used)
        LeaveTypeFactory.create_batch(4)
        with django_assert_max_num_queries(6):
            rows = {row["uid"]: row for row in hr_client.get(TYPES).json()["results"]}
        assert rows[str(used.uid)]["record_count"] == 2
        response = hr_client.delete(f"{TYPES}{used.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "leave_type_in_use"

    def test_validation(self, hr_client):
        response = hr_client.post(TYPES, {"code": "", "name": ""}, format="json")
        assert response.status_code == 400 and {"code", "name"} <= set(response.json()["errors"])


class TestAttendanceRules:
    def test_create_global_rule_recomputes_every_office(self, hr_client):
        response = hr_client.post(RULES, {"name": "Company deadline", "rules": {"half_day_after": "10:30"}}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["scope"] == "GLOBAL" and body["rules"] == {"half_day_after": "10:30"} and body["office"] is None
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["office_uid"] is None and payload["reason"] == "attendance_rule_changed"

    def test_shift_and_office_rules(self, hr_client):
        shift, office = ShiftFactory(), OfficeFactory()
        person = EmployeeFactory(shift=shift)
        body = hr_client.post(RULES, {"name": "Night", "shift": str(shift.uid), "rules": {"half_day_under_minutes": 240}, "effective_from": "2099-01-01"}, format="json").json()
        assert body["scope"] == "SHIFT" and body["shift"]["uid"] == str(shift.uid)
        assert events("hr.attendance_inputs_changed") == []  # starts in the future
        hr_client.patch(f"{RULES}{body['uid']}/", {"effective_from": None}, format="json")
        assert events("hr.attendance_inputs_changed")[-1]["employee_uids"] == [str(person.uid)]
        body = hr_client.post(RULES, {"name": "HO", "office": str(office.uid), "rules": {"half_day_after_minutes": 45}}, format="json").json()
        assert body["scope"] == "OFFICE" and events("hr.attendance_inputs_changed")[-1]["office_uid"] == str(office.uid)

    @pytest.mark.parametrize(
        "rules, key",
        [
            ({"grace_bonus": 5}, "grace_bonus"),
            ({"half_day_after": "25:99"}, "half_day_after"),
            ({"half_day_after_minutes": -5}, "half_day_after_minutes"),
            ({"half_day_after_minutes": True}, "half_day_after_minutes"),
            ({"half_day_under_minutes": 2000}, "half_day_under_minutes"),
        ],
    )
    def test_only_recognised_keys_with_valid_values(self, hr_client, rules, key):
        response = hr_client.post(RULES, {"name": "x", "rules": rules}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert key in str(response.json()["errors"]["rules"])

    def test_empty_rules_and_both_scopes_are_refused(self, hr_client):
        assert hr_client.post(RULES, {"name": "x", "rules": {}}, format="json").status_code == 400
        response = hr_client.post(RULES, {"name": "x", "office": str(OfficeFactory().uid), "shift": str(ShiftFactory().uid), "rules": {"half_day_after_minutes": 10}}, format="json")
        assert response.status_code == 400 and "shift" in response.json()["errors"]
        with pytest.raises(IntegrityError):
            AttendanceRuleFactory(office=OfficeFactory(), shift=ShiftFactory())

    def test_clock_times_are_normalised(self, hr_client):
        body = hr_client.post(RULES, {"name": "x", "rules": {"half_day_after": "8:05", "half_day_after_minutes": 20}}, format="json").json()
        assert body["rules"] == {"half_day_after": "08:05", "half_day_after_minutes": 20}

    def test_names_unique_update_stale_delete(self, hr_client):
        rule = AttendanceRuleFactory(name="Deadline")
        assert hr_client.post(RULES, {"name": "Deadline", "rules": {"half_day_after_minutes": 1}}, format="json").json()["code"] == "rule_name_taken"
        response = hr_client.patch(f"{RULES}{rule.uid}/", {"rules": {"half_day_after": "10:15"}, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["rules"] == {"half_day_after": "10:15"}
        assert hr_client.patch(f"{RULES}{rule.uid}/", {"notes": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert hr_client.delete(f"{RULES}{rule.uid}/").status_code == 204
        assert AttendanceRule.all_objects.get(pk=rule.pk).deleted_at is not None

    def test_moving_scope_recomputes_old_and_new(self, hr_client):
        old, new = OfficeFactory(), OfficeFactory()
        rule = AttendanceRuleFactory(office=old)
        hr_client.patch(f"{RULES}{rule.uid}/", {"office": str(new.uid)}, format="json")
        assert [row["office_uid"] for row in events("hr.attendance_inputs_changed")] == [str(old.uid), str(new.uid)]

    def test_list_filters_and_n_plus_one(self, hr_client, django_assert_max_num_queries):
        office, shift = OfficeFactory(), ShiftFactory()
        AttendanceRuleFactory()
        AttendanceRuleFactory(office=office)
        AttendanceRuleFactory(shift=shift, is_active=False)
        with django_assert_max_num_queries(6):
            assert hr_client.get(RULES).json()["count"] == 3
        assert hr_client.get(RULES, {"scope": "GLOBAL"}).json()["count"] == 1
        assert hr_client.get(RULES, {"scope": "OFFICE"}).json()["count"] == 1
        assert hr_client.get(RULES, {"scope": "SHIFT"}).json()["count"] == 1
        assert hr_client.get(RULES, {"office": str(office.uid)}).json()["count"] == 1
        assert hr_client.get(RULES, {"is_active": "false"}).json()["count"] == 1
