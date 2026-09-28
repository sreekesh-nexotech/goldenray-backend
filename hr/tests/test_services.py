"""Service-level cases the API tests do not reach: dashboard counters, scopes, registries, helpers, rule merging."""

import datetime as dt

import pytest
from freezegun import freeze_time

from accounts.models import Role
from accounts.tests.factories import seeded_role
from core.errors import DomainError, PermissionDenied
from hr import registries
from hr.models import AttendanceRule
from hr.services import common, employee_links, recompute, rules, scopes
from hr.tests.conftest import events
from hr.tests.factories import AttendanceRuleFactory, EmployeeFactory, LeaveRecordFactory, OfficeFactory, ShiftFactory

pytestmark = pytest.mark.django_db


class TestDashboard:
    def test_counters_follow_the_scope(self, auth_client, staff, hr_user, office):
        EmployeeFactory(office=office)
        EmployeeFactory(office=OfficeFactory(), is_active=False)
        LeaveRecordFactory(employee=staff.employee, status="PENDING")
        LeaveRecordFactory(status="PENDING")
        with freeze_time("2026-03-02T06:00:00Z"):
            LeaveRecordFactory(status="APPROVED", date_from=dt.date(2026, 3, 2), date_to=dt.date(2026, 3, 3))
            hr_body = auth_client(hr_user).get("/api/v1/dashboard/").json()
            staff_body = auth_client(staff).get("/api/v1/dashboard/").json()
        hr_counts = hr_body.get("modules", hr_body)
        staff_counts = staff_body.get("modules", staff_body)
        assert hr_counts["employees"] == {"active": 4, "inactive": 1}  # staff, the colleague and the two leave records' employees
        assert hr_counts["leave"] == {"pending": 2, "on_leave_today": 1}
        assert hr_counts["hr_setup"]["offices"] >= 1
        assert staff_counts["leave"] == {"pending": 1, "on_leave_today": 0} and "employees" not in staff_counts


class TestScopes:
    def test_own_employee_is_memoised(self, staff, django_assert_num_queries):
        scopes.forget(staff)
        with django_assert_num_queries(1):
            assert scopes.own_employee(staff) == scopes.own_employee(staff)
        assert scopes.own_employee(None) is None

    def test_employees_for_leave_scope(self, staff, manager, hr_user, make_user, office):
        colleague = EmployeeFactory(office=office)
        assert list(scopes.employees_for_module(staff, "leave")) == [staff.employee]
        assert colleague in scopes.employees_for_module(manager, "leave")
        assert colleague in scopes.employees_for_module(hr_user, "leave")
        assert not scopes.employees_for_module(make_user(grants={"leave": ["view"]}, scopes={"leave": "office"}), "leave").exists()
        assert not scopes.employees_for_module(make_user(), "leave").exists()


class TestRegistries:
    def test_names_are_validated_and_counters_flattened(self):
        with pytest.raises(ValueError):
            registries.office_summary.register("bad name!")
        sections = registries.Sections("test")
        sections.register("a")(lambda: {"x": "2"})
        sections.register("b")(lambda: {"y": 3})
        assert sections.names() == ["a", "b"] and registries.counters(sections) == {"x": 2, "y": 3}

    def test_a_missing_provider_raises(self):
        provider = registries.Provider("thing")
        assert provider.available is False
        with pytest.raises(LookupError):
            provider()


class TestHelpers:
    def test_today_in_falls_back_to_the_platform_zone(self):
        with freeze_time("2026-03-14T20:00:00Z"):
            assert common.today_in("Asia/Kolkata") == dt.date(2026, 3, 15)
            assert common.today_in("America/New_York") == dt.date(2026, 3, 14)
            assert common.today_in("Not/AZone") == dt.date(2026, 3, 15)
            assert common.today_in(None) == dt.date(2026, 3, 15)

    def test_lock_of_a_vanished_row(self):
        office = OfficeFactory()
        office.soft_delete()
        with pytest.raises(DomainError) as error:
            common.lock(type(office), office)
        assert error.value.status == 404

    def test_recompute_skips_empty_employee_lists_and_orders_ranges(self):
        recompute.for_employees([], dt.date(2026, 1, 1), dt.date(2026, 1, 2), reason="x")
        recompute.for_office(None, dt.date(2026, 1, 5), dt.date(2026, 1, 1), reason="x")
        assert events("hr.attendance_inputs_changed") == [{"office_uid": None, "date_from": "2026-01-01", "date_to": "2026-01-05", "reason": "x"}]

    def test_lookback_is_configurable(self, settings):
        settings.HR_RECOMPUTE_LOOKBACK_DAYS = 7
        start, today = recompute.recent_range()
        assert (today - start).days == 7


class TestRules:
    def test_rule_errors_report_every_problem(self):
        clean, problems = rules.rule_errors({"half_day_after": "10:00:30", "half_day_after_minutes": "5", "x": 1})
        assert clean == {"half_day_after": "10:00:30"} and set(problems) == {"half_day_after_minutes", "x"}
        assert rules.rule_errors([1]) == ({}, {"rules": "Must be an object."})

    def test_merge_order_global_office_shift(self):
        office, shift = OfficeFactory(), ShiftFactory()
        AttendanceRuleFactory(name="global", rules={"half_day_after": "10:00", "half_day_under_minutes": 200})
        AttendanceRuleFactory(name="office", office=office, rules={"half_day_after": "10:30"}, effective_from=dt.date(2025, 1, 1))
        AttendanceRuleFactory(name="shift", shift=shift, rules={"half_day_after": "11:00"})
        AttendanceRuleFactory(name="other office", office=OfficeFactory(), rules={"half_day_after": "12:00"})
        AttendanceRuleFactory(name="later", office=office, rules={"half_day_after": "10:45"}, effective_from=dt.date(2026, 6, 1))
        applicable = rules.applicable_rules(office=office, shift=shift, on=dt.date(2026, 3, 1))
        assert [rule.name for rule in applicable] == ["global", "office", "shift"]
        assert rules.merged(applicable) == {"half_day_after": "11:00", "half_day_under_minutes": 200}
        assert rules.merged(rules.applicable_rules(office=office, on=dt.date(2026, 7, 1)))["half_day_after"] == "10:45"

    def test_inactive_rule_update_that_activates_it_recomputes(self, hr_user):
        rule = AttendanceRuleFactory(is_active=False)
        rules.update_rule(rule, user=hr_user, data={"is_active": True})
        assert events("hr.attendance_inputs_changed")[-1]["office_uid"] is None
        assert AttendanceRule.objects.get(pk=rule.pk).version == 2


class TestLinks:
    def test_staff_role_is_seeded_when_missing(self):
        Role.objects.filter(slug="staff").delete()
        assert employee_links.staff_role().slug == "staff"

    def test_within_staff_grants(self, make_user):
        assert employee_links.within_staff_grants(seeded_role("staff"))
        assert employee_links.within_staff_grants(make_user(grants={"attendance": ["view"]}, scopes={"attendance": "self"}).role)
        assert not employee_links.within_staff_grants(make_user(grants={"attendance": ["view"]}, scopes={"attendance": "all"}).role)
        assert not employee_links.within_staff_grants(seeded_role("hr"))

    def test_nobody_turns_their_own_role_into_staff(self, make_user):
        actor = make_user(grants={"dashboard": ["view"], "attendance": ["view"], "leave": ["view", "create"]}, scopes={"attendance": "self", "leave": "self"})
        with pytest.raises(PermissionDenied) as error:
            employee_links.link_user(EmployeeFactory(), user=actor, data={"user_uid": actor.uid})
        assert error.value.code == "own_role_change"
