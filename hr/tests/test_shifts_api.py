"""hr/shifts/ — every PLAN §2.9 field incl. the v4 ones, cross-field checks, recompute on engine edits, delete guard."""

import datetime as dt

import pytest
from django.db import IntegrityError

from audit.models import AuditLog
from hr.models import AttendanceRule, Shift
from hr.models.shift import MINUTE_FIELDS
from hr.tests.conftest import events
from hr.tests.factories import AttendanceRuleFactory, EmployeeFactory, OfficeFactory, ShiftFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/hr/shifts/"


def detail(shift, suffix=""):
    return f"{URL}{shift.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {}, format="json").status_code == 401

    def test_view_and_edit_grants(self, auth_client, make_user):
        shift = ShiftFactory()
        viewer = auth_client(make_user(grants={"hr_setup": ["view"]}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(shift)).status_code == 200
        assert viewer.post(URL, {"code": "A", "name": "A", "start_time": "09:00", "end_time": "17:00"}, format="json").status_code == 403
        assert viewer.patch(detail(shift), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(shift)).status_code == 403
        assert auth_client(make_user(grants={"leave": "*"})).get(URL).status_code == 403


class TestCreate:
    def test_defaults_are_the_plan_values(self, hr_client):
        response = hr_client.post(URL, {"code": "GEN", "name": "General", "start_time": "09:30", "end_time": "18:30"}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert {name: body[name] for name, _, _ in MINUTE_FIELDS} == {name: default for name, default, _ in MINUTE_FIELDS}
        assert body["overnight_buffer_minutes"] == 180 and body["half_day_after_minutes"] == 30 and body["debounce_minutes"] == 2
        assert body["working_days"] == [0, 1, 2, 3, 4, 5] and body["weekly_off_days"] == [6]
        assert body["auto_deduct_break"] is True and body["overtime_enabled"] is True and body["is_overnight"] is False
        assert body["half_day_after"] == "10:00:00" and body["half_day_after_source"] == "shift"
        assert AuditLog.objects.filter(action="hr.shift_created").count() == 1

    def test_every_field_is_writable(self, hr_client):
        payload = {
            "code": "NIGHT",
            "name": "Night",
            "start_time": "22:00",
            "end_time": "06:00",
            "is_overnight": True,
            "overnight_buffer_minutes": 120,
            "grace_minutes": 5,
            "late_threshold_minutes": 10,
            "early_exit_threshold_minutes": 20,
            "full_day_minutes": 450,
            "half_day_minutes": 225,
            "half_day_after_minutes": 45,
            "break_minutes": 30,
            "auto_deduct_break": False,
            "debounce_minutes": 5,
            "overtime_enabled": False,
            "overtime_after_minutes": 500,
            "working_days": [4, 0, 1],
            "weekly_off_days": [6, 5],
            "is_active": False,
        }
        body = hr_client.post(URL, payload, format="json").json()
        expected = {**payload, "start_time": "22:00:00", "end_time": "06:00:00", "working_days": [0, 1, 4], "weekly_off_days": [5, 6]}
        assert {key: body[key] for key in payload} == expected
        assert body["half_day_after"] == "22:45:00"

    @pytest.mark.parametrize(
        "payload, field",
        [
            ({"start_time": "22:00", "end_time": "06:00"}, "end_time"),  # ends next day but not marked overnight
            ({"start_time": "09:00", "end_time": "18:00", "is_overnight": True}, "end_time"),
            ({"start_time": "09:00", "end_time": "18:00", "half_day_minutes": 500}, "half_day_minutes"),
            ({"start_time": "09:00", "end_time": "18:00", "working_days": [7]}, "working_days"),
            ({"start_time": "09:00", "end_time": "18:00", "weekly_off_days": [6, 6]}, "weekly_off_days"),
            ({"start_time": "09:00", "end_time": "18:00", "debounce_minutes": 61}, "debounce_minutes"),
            ({"start_time": "09:00", "end_time": "18:00", "grace_minutes": -1}, "grace_minutes"),
            ({"start_time": "9am", "end_time": "18:00"}, "start_time"),
        ],
    )
    def test_validation(self, hr_client, payload, field):
        response = hr_client.post(URL, {"code": "X", "name": "X", **payload}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert field in response.json()["errors"]

    def test_duplicate_code(self, hr_client):
        ShiftFactory(code="GEN")
        response = hr_client.post(URL, {"code": "gen", "name": "x", "start_time": "09:00", "end_time": "17:00"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "shift_code_taken"

    def test_db_checks_back_the_rules(self):
        with pytest.raises(IntegrityError):
            ShiftFactory(start_time=dt.time(22), end_time=dt.time(6), is_overnight=False)


class TestListAndDetail:
    def test_list_counts_and_deadline_without_n_plus_one(self, hr_client, django_assert_max_num_queries):
        shifts = [ShiftFactory() for _ in range(5)]
        for shift in shifts:
            EmployeeFactory.create_batch(2, shift=shift)
            EmployeeFactory(shift=shift, is_active=False)
        AttendanceRuleFactory(shift=shifts[0], rules={"half_day_after": "11:15"})
        AttendanceRuleFactory(rules={"half_day_after_minutes": 60})
        AttendanceRuleFactory(office=OfficeFactory(), rules={"half_day_after": "12:00"})  # office rules depend on the employee
        with django_assert_max_num_queries(8):
            rows = {row["uid"]: row for row in hr_client.get(URL).json()["results"]}
        first, second = rows[str(shifts[0].uid)], rows[str(shifts[1].uid)]
        assert first["employee_count"] == 2 and first["half_day_after"] == "11:15:00" and first["half_day_after_source"] == "rule"
        assert second["half_day_after"] == "10:30:00" and second["half_day_after_source"] == "rule"
        assert hr_client.get(URL, {"is_overnight": "true"}).json()["count"] == 0

    def test_future_and_inactive_rules_do_not_count(self, hr_client):
        shift = ShiftFactory()
        AttendanceRuleFactory(shift=shift, rules={"half_day_after": "11:15"}, effective_from=dt.date(2099, 1, 1))
        AttendanceRuleFactory(shift=shift, rules={"half_day_after": "11:45"}, is_active=False)
        body = hr_client.get(detail(shift)).json()
        assert body["half_day_after"] == "10:00:00" and body["half_day_after_source"] == "shift"


class TestUpdateAndDelete:
    def test_engine_edit_recomputes_its_people(self, hr_client):
        shift = ShiftFactory()
        own = EmployeeFactory(shift=shift)
        via_office = EmployeeFactory(office=OfficeFactory(default_shift=shift), shift=None)
        EmployeeFactory(shift=ShiftFactory())
        response = hr_client.patch(detail(shift), {"grace_minutes": 15, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["employee_uids"] == sorted([str(own.uid), str(via_office.uid)]) and payload["reason"] == "shift_updated"
        assert (dt.date.fromisoformat(payload["date_to"]) - dt.date.fromisoformat(payload["date_from"])).days == 31

    def test_renaming_recomputes_nothing(self, hr_client):
        shift = ShiftFactory()
        EmployeeFactory(shift=shift)
        assert hr_client.patch(detail(shift), {"name": "Renamed"}, format="json").status_code == 200
        assert events("hr.attendance_inputs_changed") == []

    def test_update_checks_the_resulting_row(self, hr_client):
        shift = ShiftFactory(start_time=dt.time(9), end_time=dt.time(17))
        response = hr_client.patch(detail(shift), {"start_time": "18:00"}, format="json")
        assert response.status_code == 400 and "end_time" in response.json()["errors"]
        assert hr_client.patch(detail(shift), {"start_time": "18:00", "end_time": "02:00", "is_overnight": True}, format="json").status_code == 200

    def test_stale_version(self, hr_client):
        shift = ShiftFactory(version=2)
        response = hr_client.patch(detail(shift), {"name": "x", "expected_version": 1}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_delete_is_refused_while_in_use(self, hr_client):
        shift = ShiftFactory()
        EmployeeFactory(shift=shift)
        OfficeFactory(default_shift=shift)
        response = hr_client.delete(detail(shift))
        assert response.status_code == 409 and response.json()["code"] == "shift_in_use"
        assert response.json()["errors"] == {"employees": ["1"], "offices": ["1"]}

    def test_delete_takes_its_rules(self, hr_client):
        shift = ShiftFactory()
        rule = AttendanceRuleFactory(shift=shift)
        assert hr_client.delete(detail(shift)).status_code == 204
        assert Shift.all_objects.get(pk=shift.pk).deleted_at and AttendanceRule.all_objects.get(pk=rule.pk).deleted_at
