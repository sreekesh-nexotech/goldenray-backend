"""``attendance/dashboard/*`` (scoped, eSSL §I.1) and what attendance adds to hr's screens and ``GET dashboard/``."""

from __future__ import annotations

import datetime as dt

import pytest

from attendance.services import recompute
from attendance.tests.conftest import at
from attendance.tests.factories import AttendanceCorrectionFactory, AttendanceDayFactory, punch
from hr.tests.factories import EmployeeFactory

pytestmark = pytest.mark.django_db

SUMMARY = "/api/v1/attendance/dashboard/summary/"
RECENT = "/api/v1/attendance/dashboard/recent-punches/"
TREND = "/api/v1/attendance/dashboard/trend/"


@pytest.fixture
def busy(world):
    punch(world.d1, "1", at(15, 9, 31))  # Asha in today, no OUT yet
    punch(world.d1, "2", at(14, 9, 30))
    punch(world.d1, "2", at(14, 18, 30))
    punch(world.d2, "1", at(15, 8, 55))  # Chitra (Dubai clock)
    recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
    return world


class TestSummary:
    def test_permissions(self, api_client, auth_client, make_user, world):
        assert api_client.get(SUMMARY).status_code == 401
        assert auth_client(make_user(grants={"devices": ["view"]})).get(SUMMARY).status_code == 403

    def test_today_everyone_with_devices(self, auth_client, make_user, busy, django_assert_max_num_queries):
        admin = make_user(grants={"attendance": ["view"], "devices": ["view"]}, scopes={"attendance": "all"})
        client = auth_client(admin)
        with django_assert_max_num_queries(45):
            response = client.get(SUMMARY)
        body = response.json()
        assert response.status_code == 200 and body["date"] == "2026-09-15" and body["provisional"] is True and body["scope"] == "all"
        assert body["overall"]["present_days"] == 2 and body["overall"]["currently_in"] == 2 and body["overall"]["pending"] == 2 and body["overall"]["punches"] == 2
        blocks = {block["office"]["code"]: block for block in body["offices"]}
        assert blocks["HO"]["punches"] == 1 and blocks["HO"]["counts"]["present"] == 1 and "connection" in blocks["HO"]["devices"]

    def test_scopes_and_no_device_block_without_devices_view(self, staff_client, manager_client, busy):
        own = staff_client.get(SUMMARY).json()
        assert own["scope"] == "self" and own["overall"]["total"] == 1 and own["overall"]["punches"] == 1
        assert [block["office"]["code"] for block in own["offices"]] == ["HO"] and "devices" not in own["offices"][0]
        office = manager_client.get(SUMMARY).json()
        assert office["scope"] == "office" and office["overall"]["total"] == 2 and [block["office"]["code"] for block in office["offices"]] == ["BR"]

    def test_a_final_day(self, hr_client, busy):
        body = hr_client.get(SUMMARY, {"day": "2026-09-14", "office": str(busy.ho.uid)}).json()
        assert body["provisional"] is False and body["overall"]["present"] == 1 and body["overall"]["absent"] == 1
        assert hr_client.get(SUMMARY, {"day": "x"}).status_code == 400


class TestRecentPunches:
    def test_scoped_newest_first_with_names(self, hr_client, staff_client, busy, django_assert_max_num_queries):
        with django_assert_max_num_queries(10):
            rows = hr_client.get(RECENT, {"limit": 3}).json()
        # newest by the instant: 08:55 in Dubai (04:55 UTC) is later than 09:31 in Kolkata (04:01 UTC)
        assert [(row["punch"]["device_time"], row["employee"]["code"]) for row in rows] == [("2026-09-15T08:55:00", "E003"), ("2026-09-15T09:31:00", "E001"), ("2026-09-14T18:30:00", "E002")]
        own = staff_client.get(RECENT).json()
        assert {row["employee"]["code"] for row in own} == {"E001"}
        assert hr_client.get(RECENT, {"office": str(busy.br.uid)}).json()[0]["employee"]["code"] == "E003"

    def test_limits(self, hr_client, world):
        assert hr_client.get(RECENT, {"limit": 0}).status_code == 400
        assert hr_client.get(RECENT, {"limit": 201}).status_code == 400


class TestTrend:
    def test_daily_counts_up_to_today(self, hr_client, busy):
        rows = hr_client.get(TREND, {"days": 3}).json()
        assert [row["date"] for row in rows] == ["2026-09-13", "2026-09-14", "2026-09-15"]
        assert rows[0]["weekly_off"] == 4 and rows[1]["present"] == 1 and rows[2]["pending"] == 4  # today: pending, never absent
        assert hr_client.get(TREND, {"days": 91}).status_code == 400
        assert len(hr_client.get(TREND).json()) == 7


class TestProviders:
    def test_hr_office_summary_section(self, auth_client, make_user, busy):
        viewer = make_user(grants={"hr_setup": ["view"], "attendance": ["view"]}, scopes={"attendance": "all"})
        body = auth_client(viewer).get(f"/api/v1/hr/offices/{busy.ho.uid}/summary/", {"day": "2026-09-14"}).json()
        assert body["sections"]["attendance"]["present"] == 1 and body["sections"]["attendance"]["date"] == "2026-09-14"
        blind = make_user(grants={"hr_setup": ["view"]})
        assert "attendance" not in auth_client(blind).get(f"/api/v1/hr/offices/{busy.ho.uid}/summary/").json()["sections"]

    def test_employee_dependencies_block_deleting_history(self, hr_client, busy):
        body = hr_client.get(f"/api/v1/hr/employees/{busy.binu.uid}/dependencies/").json()
        assert body["counts"]["attendance_days"] == 1 and body["counts"]["raw_punches"] == 2 and body["can_delete"] is False
        fresh = EmployeeFactory(office=busy.ho)
        assert hr_client.get(f"/api/v1/hr/employees/{fresh.uid}/dependencies/").json()["counts"]["attendance_days"] == 0

    def test_dashboard_counters(self, hr_client, staff_client, busy):
        AttendanceCorrectionFactory(day=AttendanceDayFactory(employee=busy.chitra, work_date=dt.date(2026, 9, 1)))
        counters = hr_client.get("/api/v1/dashboard/").json()
        attendance = counters["modules"]["attendance"] if "modules" in counters else counters["attendance"]
        assert attendance["present_yesterday"] == 1 and attendance["absent_yesterday"] == 3 and attendance["active_corrections"] == 1
        own = staff_client.get("/api/v1/dashboard/").json()
        own = own["modules"]["attendance"] if "modules" in own else own["attendance"]
        assert own["absent_yesterday"] == 1 and own["active_corrections"] == 0
