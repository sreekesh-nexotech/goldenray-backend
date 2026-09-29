"""hr/offices/ — CRUD (hr_setup), zoneinfo validation, attendance settings, summary registry, delete guard."""

import datetime as dt

import pytest
from freezegun import freeze_time

from audit.models import AuditLog
from hr import registries
from hr.models import AttendanceRule, Holiday, Office
from hr.tests.conftest import events
from hr.tests.factories import AttendanceRuleFactory, EmployeeFactory, HolidayFactory, LeaveRecordFactory, OfficeFactory, ShiftFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/hr/offices/"


def detail(office, suffix=""):
    return f"{URL}{office.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        office = OfficeFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {"code": "X", "name": "X"}, format="json").status_code == 401
        assert api_client.get(detail(office, "summary/")).status_code == 401

    def test_view_and_edit_grants(self, auth_client, make_user):
        office = OfficeFactory()
        viewer = auth_client(make_user(grants={"hr_setup": ["view"]}))
        assert viewer.get(URL).status_code == 200
        assert viewer.get(detail(office)).status_code == 200
        assert viewer.get(detail(office, "summary/")).status_code == 200
        assert viewer.post(URL, {"code": "X", "name": "X"}, format="json").status_code == 403
        assert viewer.patch(detail(office), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(office)).status_code == 403
        assert auth_client(make_user(grants={"employees": "*"})).get(URL).status_code == 403

    def test_scope_is_all(self, auth_client, make_user):
        OfficeFactory.create_batch(3)
        assert auth_client(make_user(grants={"hr_setup": ["view"]})).get(URL).json()["count"] == 3


class TestCreateAndList:
    def test_create_validates_the_time_zone_and_audits(self, hr_client, hr_user):
        shift = ShiftFactory(code="GEN")
        response = hr_client.post(URL, {"code": "SALES", "name": "Sales Office", "timezone": "Asia/Dubai", "default_shift": str(shift.uid)}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["timezone"] == "Asia/Dubai" and body["default_shift"]["code"] == "GEN" and body["version"] == 1
        assert body["attendance_settings"]["grace_minutes"] == 10 and body["attendance_settings"]["working_days"] == [0, 1, 2, 3, 4, 5]
        assert AuditLog.objects.get(action="hr.office_created").actor == hr_user

    @pytest.mark.parametrize("zone", ["India Standard Time", "Mars/Olympus", "", "../etc/passwd"])
    def test_unknown_time_zones_are_refused(self, hr_client, zone):
        response = hr_client.post(URL, {"code": "X", "name": "X", "timezone": zone}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert "timezone" in response.json()["errors"]

    def test_validation_envelope(self, hr_client):
        response = hr_client.post(URL, {"code": "", "name": ""}, format="json")
        assert response.status_code == 400
        assert response.json()["code"] == "validation_error" and {"code", "name"} <= set(response.json()["errors"])

    def test_codes_are_unique_case_insensitive_among_live_rows(self, hr_client):
        OfficeFactory(code="HO")
        clash = hr_client.post(URL, {"code": "ho", "name": "Other"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "office_code_taken"
        Office.objects.get(code="HO").soft_delete()
        assert hr_client.post(URL, {"code": "ho", "name": "Other"}, format="json").status_code == 201

    def test_list_counts_active_employees_without_n_plus_one(self, hr_client, django_assert_max_num_queries):
        shift = ShiftFactory()
        for index in range(6):
            office = OfficeFactory(default_shift=shift if index % 2 else None)
            EmployeeFactory.create_batch(2, office=office)
            EmployeeFactory(office=office, is_active=False)
        with django_assert_max_num_queries(8):
            rows = hr_client.get(URL).json()["results"]
        assert len(rows) == 6 and {row["employee_count"] for row in rows} == {2}
        assert hr_client.get(URL, {"search": rows[0]["code"]}).json()["count"] == 1
        OfficeFactory(is_active=False)
        assert hr_client.get(URL, {"is_active": "false"}).json()["count"] == 1


class TestUpdateAndDelete:
    def test_update_with_expected_version(self, hr_client):
        office = OfficeFactory(name="Branch")
        response = hr_client.patch(detail(office), {"name": "Branch 1", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        entry = AuditLog.objects.get(action="hr.office_updated")
        assert entry.before == {"name": "Branch"} and entry.after == {"name": "Branch 1"}

    def test_stale_version(self, hr_client):
        office = OfficeFactory(version=3)
        response = hr_client.patch(detail(office), {"name": "x", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert hr_client.delete(f"{detail(office)}?expected_version=2").status_code == 409

    def test_noop_update_keeps_the_version(self, hr_client):
        office = OfficeFactory(name="Same")
        assert hr_client.patch(detail(office), {"name": "Same"}, format="json").json()["version"] == 1

    def test_default_shift_change_recomputes_people_without_their_own_shift(self, hr_client):
        office = OfficeFactory()
        floating = EmployeeFactory(office=office)
        EmployeeFactory(office=office, shift=ShiftFactory())
        response = hr_client.patch(detail(office), {"default_shift": str(ShiftFactory().uid)}, format="json")
        assert response.status_code == 200
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["employee_uids"] == [str(floating.uid)] and payload["reason"] == "office_default_shift_changed"

    def test_time_zone_change_recomputes_everyone_in_the_office(self, hr_client):
        office = OfficeFactory()
        people = EmployeeFactory.create_batch(2, office=office)
        assert hr_client.patch(detail(office), {"timezone": "Asia/Dubai"}, format="json").status_code == 200
        [payload] = events("hr.attendance_inputs_changed")
        assert payload["employee_uids"] == sorted(str(person.uid) for person in people)

    def test_delete_is_refused_while_employees_belong_to_it(self, hr_client):
        office = OfficeFactory()
        EmployeeFactory(office=office, is_active=False)
        response = hr_client.delete(detail(office))
        assert response.status_code == 409 and response.json()["code"] == "office_in_use" and response.json()["errors"]["employees"] == ["1"]

    def test_delete_is_refused_while_other_packages_depend_on_it(self, hr_client):
        office = OfficeFactory()
        registries.office_dependencies.register("devices")(lambda row: {"devices": 2})
        try:
            response = hr_client.delete(detail(office))
        finally:
            registries.office_dependencies.unregister("devices")
        assert response.status_code == 409 and response.json()["errors"]["devices"] == ["2"]

    def test_delete_soft_deletes_the_office_its_holidays_and_rules(self, hr_client):
        office = OfficeFactory()
        holiday = HolidayFactory(office=office)
        rule = AttendanceRuleFactory(office=office)
        assert hr_client.delete(detail(office)).status_code == 204
        assert Office.all_objects.get(pk=office.pk).deleted_at and Holiday.all_objects.get(pk=holiday.pk).deleted_at and AttendanceRule.all_objects.get(pk=rule.pk).deleted_at
        assert hr_client.get(detail(office)).status_code == 404
        assert {"hr.office_deleted", "hr.holiday_deleted", "hr.attendance_rule_deleted"} <= set(AuditLog.objects.values_list("action", flat=True))


class TestSummary:
    def test_summary_for_a_day(self, hr_client):
        shift = ShiftFactory(weekly_off_days=[6])
        office = OfficeFactory(default_shift=shift, timezone="Asia/Kolkata")
        people = EmployeeFactory.create_batch(3, office=office)
        EmployeeFactory(office=office, is_active=False)
        LeaveRecordFactory(employee=people[0], status="APPROVED", date_from=dt.date(2026, 3, 9), date_to=dt.date(2026, 3, 11))
        LeaveRecordFactory(employee=people[1], status="PENDING", date_from=dt.date(2026, 3, 10), date_to=dt.date(2026, 3, 10))
        HolidayFactory(office=None, date=dt.date(2026, 3, 10), name="Global day")
        holiday = HolidayFactory(office=office, date=dt.date(2026, 3, 10), name="Foundation Day")
        body = hr_client.get(detail(office, "summary/"), {"day": "2026-03-10"}).json()
        assert body["date"] == "2026-03-10" and body["office"]["code"] == office.code
        assert body["employees"] == {"total": 4, "active": 3}
        assert body["leave"] == {"on_leave": 1, "pending": 1}
        assert body["holiday"] == {"uid": str(holiday.uid), "name": "Foundation Day", "is_global": False}
        assert body["is_weekly_off"] is False and body["is_working_day"] is False and body["sections"] == {}

    def test_weekly_off_and_default_day_in_the_office_zone(self, auth_client, hr_user):
        office = OfficeFactory(default_shift=ShiftFactory(weekly_off_days=[6]), timezone="Asia/Kolkata")
        with freeze_time("2026-03-14T20:00:00Z"):  # already Sunday 01:30 in Kolkata
            body = auth_client(hr_user).get(detail(office, "summary/")).json()
        assert body["date"] == "2026-03-15" and body["is_weekly_off"] is True and body["is_working_day"] is False

    def test_sections_come_from_the_registry(self, hr_client, hr_user):
        office = OfficeFactory()
        seen = []

        def attendance(row, day, user):
            seen.append((row.pk, day, user.pk))
            return {"present": 7}

        registries.office_summary.register("attendance")(attendance)
        registries.office_summary.register("broken")(lambda *args: 1 / 0)
        registries.office_summary.register("hidden")(lambda *args: None)
        try:
            body = hr_client.get(detail(office, "summary/"), {"day": "2026-03-10"}).json()
        finally:
            for name in ("attendance", "broken", "hidden"):
                registries.office_summary.unregister(name)
        assert body["sections"] == {"attendance": {"present": 7}}
        assert seen == [(office.pk, dt.date(2026, 3, 10), hr_user.pk)]

    @pytest.mark.parametrize(
        "shift_kwargs, day, weekly_off",
        [
            (None, "2026-03-15", True),  # no default shift: Sunday is the only day off (eSSL C3, what the engine does)
            (None, "2026-03-14", False),
            ({"working_days": [], "weekly_off_days": [6]}, "2026-03-11", False),  # empty working_days: every day but the weekly off
            ({"working_days": [0, 1, 2, 3, 4], "weekly_off_days": [6]}, "2026-03-14", True),  # a day outside working_days is off
            ({"working_days": [0, 1, 2, 3, 4, 5, 6], "weekly_off_days": [6]}, "2026-03-15", True),  # weekly off wins
        ],
    )
    def test_working_day_follows_the_engine_rule(self, hr_client, shift_kwargs, day, weekly_off):
        office = OfficeFactory(default_shift=ShiftFactory(**shift_kwargs) if shift_kwargs is not None else None)
        body = hr_client.get(detail(office, "summary/"), {"day": day}).json()
        assert (body["is_weekly_off"], body["is_working_day"]) == (weekly_off, not weekly_off)

    def test_bad_day_is_a_validation_error(self, hr_client):
        response = hr_client.get(detail(OfficeFactory(), "summary/"), {"day": "10-03-2026"})
        assert response.status_code == 400 and "day" in response.json()["errors"]
