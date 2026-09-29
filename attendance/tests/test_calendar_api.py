"""``attendance/calendar/``, ``…/calendar/all/``, ``attendance/day/``, ``attendance/date-ranges/`` — one calendar fill (A9),
blank today and later (A7), today provisional from the punches so far."""

from __future__ import annotations

import datetime as dt

import pytest

from attendance.models import AttendanceDay
from attendance.services import calendar, recompute
from attendance.tests.conftest import at
from attendance.tests.factories import punch
from hr.tests.factories import EmployeeFactory, HolidayFactory, LeaveRecordFactory

pytestmark = pytest.mark.django_db

CALENDAR = "/api/v1/attendance/calendar/"
ROSTER = "/api/v1/attendance/calendar/all/"
DAY = "/api/v1/attendance/day/"
RANGES = "/api/v1/attendance/date-ranges/"


class TestCalendar:
    def test_every_date_filled_and_the_future_blank(self, hr_client, world):
        punch(world.d1, "1", at(14, 9, 30))
        punch(world.d1, "1", at(14, 18, 30))
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        HolidayFactory(date=dt.date(2026, 9, 10), name="Onam", office=None)
        LeaveRecordFactory(employee=world.asha, date_from=dt.date(2026, 9, 11), date_to=dt.date(2026, 9, 11), status="APPROVED")
        response = hr_client.get(CALENDAR, {"employee": str(world.asha.uid), "year": 2026, "month": 9})
        assert response.status_code == 200
        body = response.json()
        days = {day["date"]: day for day in body["days"]}
        assert len(days) == 30 and body["month_label"] == "September 2026" and body["shift"]["code"] == "GEN"
        assert (days["2026-09-14"]["status_code"], days["2026-09-14"]["fill"], days["2026-09-14"]["first_in"], days["2026-09-14"]["working_hours"]) == ("P", "STORED", "09:30", "8:00")
        assert (days["2026-09-13"]["status_code"], days["2026-09-13"]["fill"]) == ("WO", "FILLED")
        assert (days["2026-09-10"]["status_code"], days["2026-09-10"]["holiday_name"]) == ("H", "Onam")
        assert days["2026-09-11"]["status_code"] == "L"
        assert days["2026-09-09"]["status_code"] == "A"
        for future in ("2026-09-15", "2026-09-30"):  # A7: never ABSENT
            assert (days[future]["status"], days[future]["status_code"], days[future]["fill"]) == ("", "", "PENDING")
        summary = body["summary"]
        assert summary["present"] == 1 and summary["pending_days"] == 16 and summary["leave"] == 1 and summary["holiday"] == 1
        assert summary["attendance_rate"] == f"{1 / (1 + summary['absent']):.4f}"

    def test_scope_validation_and_permissions(self, hr_client, staff_client, api_client, auth_client, make_user, world):
        assert staff_client.get(CALENDAR, {"employee": str(world.binu.uid), "year": 2026, "month": 9}).status_code == 404
        assert staff_client.get(CALENDAR, {"employee": str(world.asha.uid), "year": 2026, "month": 9}).status_code == 200
        response = hr_client.get(CALENDAR, {"employee": str(world.asha.uid), "year": 2026, "month": 13})
        assert response.status_code == 400 and "month" in response.json()["errors"]
        assert api_client.get(CALENDAR).status_code == 401
        assert auth_client(make_user(grants={"leave": ["view"]})).get(CALENDAR, {"employee": str(world.asha.uid), "year": 2026, "month": 9}).status_code == 403

    def test_not_employed_days_are_blank(self, hr_client, world):
        newcomer = EmployeeFactory(office=world.ho, joined_on=dt.date(2026, 9, 10))
        days = hr_client.get(CALENDAR, {"employee": str(newcomer.uid), "year": 2026, "month": 9}).json()["days"]
        assert days[0]["fill"] == "NOT_EMPLOYED" and days[0]["status"] == ""


class TestRoster:
    def test_every_employees_month_with_totals(self, hr_client, world, django_assert_max_num_queries):
        punch(world.d1, "1", at(14, 9, 30))
        recompute.recompute(date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 9, 14), reason="test")
        with django_assert_max_num_queries(14):
            response = hr_client.get(ROSTER, {"year": 2026, "month": 9})
        body = response.json()
        assert response.status_code == 200 and body["totals"]["employees"] == 4 and body["totals"]["days"] == 30
        assert [row["employee"]["code"] for row in body["employees"]] == ["E001", "E002", "E003", "M001"]
        by_date = {column["date"]: column for column in body["by_date"]}
        assert by_date["2026-09-14"]["present"] == 1 and by_date["2026-09-14"]["stored"] == 4 and by_date["2026-09-13"]["weekly_off"] == 4
        assert by_date["2026-09-20"]["absent"] == 0 and by_date["2026-09-20"]["stored"] == 0
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "office": str(world.br.uid)}).json()["totals"]["employees"] == 2
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "search": "binu"}).json()["totals"]["employees"] == 1

    def test_at_most_300_employees_never_truncated(self, hr_client, world, monkeypatch):
        monkeypatch.setattr(calendar, "MAX_ROSTER_EMPLOYEES", 3)
        response = hr_client.get(ROSTER, {"year": 2026, "month": 9})
        assert response.status_code == 400 and response.json()["code"] == "too_many_employees"
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "office": str(world.ho.uid)}).status_code == 200

    def test_scopes_and_inactive(self, manager_client, staff_client, hr_client, world):
        assert {row["employee"]["code"] for row in manager_client.get(ROSTER, {"year": 2026, "month": 9}).json()["employees"]} == {"E003", "M001"}
        assert {row["employee"]["code"] for row in staff_client.get(ROSTER, {"year": 2026, "month": 9}).json()["employees"]} == {"E001"}
        world.binu.is_active = False
        world.binu.save()
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9}).json()["totals"]["employees"] == 3
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "include_inactive": "true"}).json()["totals"]["employees"] == 4
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "employee": str(world.binu.uid)}).json()["totals"]["employees"] == 1
        assert hr_client.get(ROSTER, {"year": 2026, "month": 9, "office": "00000000-0000-0000-0000-000000000000"}).status_code == 400


class TestDay:
    def test_a_final_day_for_everybody_with_a_reason(self, hr_client, world):
        punch(world.d1, "1", at(14, 9, 30))
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), employee_ids=[world.asha.pk], reason="test")
        body = hr_client.get(DAY, {"day": "2026-09-14"}).json()
        cells = {row["employee"]["code"]: row for row in body["rows"]}
        assert cells["E001"]["cell"]["fill"] == "STORED" and cells["E001"]["day_uid"] == str(AttendanceDay.objects.get(employee=world.asha).uid)
        assert cells["E002"]["cell"]["status"] == "ABSENT" and cells["E002"]["cell"]["fill"] == "FILLED" and cells["E002"]["day_uid"] is None
        assert body["counts"]["total"] == 4 and body["rows_without_stored_record"] == 3 and body["day_name"] == "Monday"

    def test_today_is_provisional_from_the_punches_so_far(self, hr_client, world):
        punch(world.d1, "1", at(15, 9, 31))
        punch(world.d1, "2", at(15, 11, 0))
        punch(world.d1, "2", at(15, 13, 0))  # later than now (12:00 in Kolkata): not counted yet
        body = hr_client.get(DAY).json()
        assert body["date"] == "2026-09-15"
        cells = {row["employee"]["code"]: row["cell"] for row in body["rows"]}
        assert (cells["E001"]["fill"], cells["E001"]["status"], cells["E001"]["last_out"]) == ("PROVISIONAL", "PRESENT", "Missing OUT")
        assert (cells["E002"]["fill"], cells["E002"]["status"], cells["E002"]["punch_count"]) == ("PROVISIONAL", "HALF_DAY", 1)
        assert (cells["M001"]["fill"], cells["M001"]["status"]) == ("PENDING", "")  # not in yet: never ABSENT
        assert not AttendanceDay.objects.exists()  # nothing stored before the day is final
        assert body["counts"]["pending"] >= 1 and body["counts"]["present_days"] == 2

    def test_filters_scope_and_validation(self, hr_client, staff_client, world):
        assert {row["employee"]["code"] for row in staff_client.get(DAY).json()["rows"]} == {"E001"}
        assert {row["employee"]["code"] for row in hr_client.get(DAY, {"office": str(world.br.uid)}).json()["rows"]} == {"E003", "M001"}
        assert hr_client.get(DAY, {"day": "tomorrow"}).status_code == 400
        future = hr_client.get(DAY, {"day": "2026-09-20"}).json()
        assert {row["cell"]["fill"] for row in future["rows"]} == {"PENDING"}


class TestDateRanges:
    def test_office_local_dates(self, auth_client, world, frozen):
        frozen.move_to("2026-09-14T21:00:00+00:00")  # 02:30 on the 15th in Kolkata, 01:00 on the 15th in Dubai
        hr_client, staff_client = auth_client(world.hr), auth_client(world.staff)  # tokens issued on the moved clock
        body = hr_client.get(RANGES, {"office": str(world.ho.uid)}).json()
        assert body["timezone"] == "Asia/Kolkata" and body["today"] == "2026-09-15" and body["office"] == str(world.ho.uid)
        assert body["ranges"]["last7"] == {"date_from": "2026-09-09", "date_to": "2026-09-15"}
        assert body["ranges"]["yesterday"] == {"date_from": "2026-09-14", "date_to": "2026-09-14"}
        own = staff_client.get(RANGES).json()  # the caller's own office
        assert own["office"] == str(world.ho.uid) and own["today"] == "2026-09-15"
        platform = hr_client.get(RANGES).json()
        assert platform["office"] is None and platform["timezone"] == "Asia/Kolkata"
        frozen.move_to("2026-09-14T19:00:00+00:00")  # 00:30 in Kolkata, 23:00 on the 14th in Dubai
        assert auth_client(world.hr).get(RANGES, {"office": str(world.br.uid)}).json()["today"] == "2026-09-14"
