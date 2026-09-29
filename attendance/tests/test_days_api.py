"""``attendance/days/``, ``attendance/raw/`` (cursor), ``attendance/employees/<uid>/timeline/``."""

from __future__ import annotations

import datetime as dt

import pytest

from attendance.services import recompute
from attendance.tests.conftest import at
from attendance.tests.factories import AttendanceDayFactory, punch
from hr.tests.factories import EmployeeFactory

pytestmark = pytest.mark.django_db

DAYS = "/api/v1/attendance/days/"
RAW = "/api/v1/attendance/raw/"


def timeline_url(employee) -> str:
    return f"/api/v1/attendance/employees/{employee.uid}/timeline/"


@pytest.fixture
def computed(world):
    punch(world.d1, "1", at(14, 9, 45))
    punch(world.d1, "1", at(14, 18, 30))
    punch(world.d1, "2", at(14, 10, 15))
    punch(world.d2, "1", at(14, 9, 0))
    recompute.recompute(date_from=dt.date(2026, 9, 13), date_to=dt.date(2026, 9, 14), reason="test")
    return world


class TestDays:
    def test_anonymous_and_missing_permission(self, api_client, auth_client, make_user, world):
        assert api_client.get(DAYS).status_code == 401
        response = auth_client(make_user(grants={"employees": ["view"]})).get(DAYS)
        assert response.status_code == 403 and response.json()["code"] == "permission_denied"

    def test_list_with_origins_and_filters(self, hr_client, computed, django_assert_max_num_queries):
        with django_assert_max_num_queries(12):
            response = hr_client.get(DAYS, {"date_from": "2026-09-14", "date_to": "2026-09-14", "ordering": "employee__full_name"})
        assert response.status_code == 200
        rows = response.json()["results"]
        assert [row["employee"]["code"] for row in rows] == ["E001", "E002", "E003", "M001"]
        asha = rows[0]
        assert (asha["status"], asha["status_code"], asha["status_label"], asha["first_in"], asha["last_out"], asha["late_minutes"]) == (
            "LATE",
            "LT",
            "Late",
            "2026-09-14T09:45:00",
            "2026-09-14T18:30:00",
            5,
        )
        assert asha["origin"] == {"sources": ["AGENT_PUSH"], "devices": ["HO-01 · NCD0000000001"], "offices": ["Head Office"]}
        assert asha["first_device"]["name"] == "HO-01" and asha["office"]["code"] == "HO" and asha["shift"]["code"] == "GEN"
        assert rows[3]["origin"] is None and rows[3]["status"] == "ABSENT"
        assert hr_client.get(DAYS, {"status": "WEEKLY_OFF"}).json()["count"] == 4
        assert hr_client.get(DAYS, {"employee": str(computed.binu.uid), "date_from": "2026-09-14"}).json()["results"][0]["status"] == "HALF_DAY"
        assert hr_client.get(DAYS, {"office": str(computed.br.uid)}).json()["count"] == 4
        assert hr_client.get(DAYS, {"search": "chitra"}).json()["count"] == 2
        assert hr_client.get(DAYS, {"filter[status]": "HALF_DAY"}).json()["count"] == 1

    def test_scopes(self, staff_client, manager_client, computed):
        own = staff_client.get(DAYS).json()["results"]
        assert {row["employee"]["code"] for row in own} == {"E001"}
        office = manager_client.get(DAYS).json()["results"]
        assert {row["employee"]["code"] for row in office} == {"E003", "M001"}
        someone_else = computed.binu.attendance_days.first()
        assert staff_client.get(f"{DAYS}{someone_else.uid}/").status_code == 404

    def test_inactive_employees_only_on_request(self, hr_client, computed):
        computed.binu.is_active = False
        computed.binu.save()
        assert "E002" not in {row["employee"]["code"] for row in hr_client.get(DAYS).json()["results"]}
        assert "E002" in {row["employee"]["code"] for row in hr_client.get(DAYS, {"include_inactive": "true"}).json()["results"]}

    def test_retrieve_and_validation(self, hr_client, computed):
        day = computed.asha.attendance_days.get(work_date=dt.date(2026, 9, 14))
        body = hr_client.get(f"{DAYS}{day.uid}/").json()
        assert body["uid"] == str(day.uid) and body["origin"]["devices"] == ["HO-01 · NCD0000000001"] and body["version"] == day.version
        response = hr_client.get(DAYS, {"date_from": "not-a-date"})
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and "date_from" in response.json()["errors"]
        assert hr_client.get(DAYS, {"status": "NOPE"}).status_code == 400

    def test_a_missing_out_is_stated_never_invented(self, hr_client, computed):
        chitra = hr_client.get(DAYS, {"employee": str(computed.chitra.uid), "date_from": "2026-09-14"}).json()["results"][0]
        assert chitra["last_out"] is None and chitra["missing_out"] is True
        assert hr_client.get(DAYS, {"missing_out": "true"}).json()["count"] == 2


class TestRaw:
    def test_permissions(self, api_client, auth_client, make_user, world):
        assert api_client.get(RAW).status_code == 401
        assert auth_client(make_user(grants={"devices": ["view"]})).get(RAW).status_code == 403

    def test_cursor_list_with_people(self, hr_client, computed, django_assert_max_num_queries):
        with django_assert_max_num_queries(8):
            response = hr_client.get(RAW, {"page_size": 2})
        body = response.json()
        assert response.status_code == 200 and set(body) == {"results", "next", "previous"} and body["next"]
        first = body["results"][0]
        assert first["device_time"] == "2026-09-14T18:30:00" and first["employee"]["code"] == "E001" and len(first["dedup_key"]) == 64
        assert "id" not in first
        second = hr_client.get(body["next"]).json()
        assert len(second["results"]) == 2 and second["next"] is None

    def test_filters(self, hr_client, computed):
        def count(**params):
            return len(hr_client.get(RAW, params).json()["results"])

        assert count(employee=str(computed.asha.uid)) == 2  # PIN 1 on HO-01 only (A1)
        assert count(pin="1") == 3
        assert count(device=str(computed.d2.uid)) == 1
        assert count(date_from="2026-09-15") == 0 and count(date_to="2026-09-14", date_from="2026-09-14") == 4
        assert count(employee=str(EmployeeFactory().uid)) == 0
        assert hr_client.get(RAW, {"date_from": "x"}).status_code == 400

    def test_scopes(self, staff_client, manager_client, computed):
        own = staff_client.get(RAW).json()["results"]
        assert {row["employee"]["code"] for row in own} == {"E001"} and len(own) == 2
        office = manager_client.get(RAW).json()["results"]
        assert {row["employee"]["code"] for row in office} == {"E003"}


class TestTimeline:
    def test_the_day_and_every_punch_behind_it(self, hr_client, computed):
        punch(computed.d1, "1", at(14, 9, 46))  # a double scan
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        punch(computed.d1, "1", at(13, 11, 0))  # a weekly-off punch on the day before
        body = hr_client.get(timeline_url(computed.asha), {"work_date": "2026-09-14"}).json()
        assert body["employee"]["code"] == "E001" and body["timezone"] == "Asia/Kolkata" and body["day"]["status"] == "LATE"
        flags = [(row["office_time"], row["accepted"], row["ignored"], row["in_day"]) for row in body["punches"]]
        assert flags == [
            ("2026-09-13T11:00:00", False, False, False),
            ("2026-09-14T09:45:00", True, False, True),
            ("2026-09-14T09:46:00", False, True, True),
            ("2026-09-14T18:30:00", True, False, True),
        ]
        assert body["punches"][0]["work_date"] == "2026-09-13"

    def test_no_day_yet_and_errors(self, hr_client, staff_client, api_client, world):
        body = hr_client.get(timeline_url(world.asha), {"work_date": "2026-09-15"}).json()
        assert body["day"] is None and body["punches"] == []
        assert hr_client.get(timeline_url(world.asha)).status_code == 400
        assert staff_client.get(timeline_url(world.binu), {"work_date": "2026-09-14"}).status_code == 404
        assert staff_client.get(timeline_url(world.asha), {"work_date": "2026-09-14"}).status_code == 200
        assert api_client.get(timeline_url(world.asha), {"work_date": "2026-09-14"}).status_code == 401

    def test_an_employee_without_links(self, hr_client, world):
        loner = EmployeeFactory(office=world.ho)
        AttendanceDayFactory(employee=loner, work_date=dt.date(2026, 9, 14), status="ABSENT", first_in=None, last_out=None, punch_count=0, working_minutes=0, break_minutes=0)
        body = hr_client.get(timeline_url(loner), {"work_date": "2026-09-14"}).json()
        assert body["day"]["status"] == "ABSENT" and body["day"]["origin"] is None and body["punches"] == []
