"""Defects found by the adversarial review of the attendance package (each test failed before its fix)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from attendance.models import AttendanceDay
from attendance.services import legacy_import, recompute
from attendance.tests.conftest import at
from attendance.tests.factories import punch
from devices.services import legacy_import as devices_import
from devices.tests.factories import DeviceFactory, DeviceUserFactory
from hr.models import Employee
from hr.services import legacy_import as hr_import
from hr.tests.factories import EmployeeFactory, OfficeFactory

pytestmark = pytest.mark.django_db
IMPORT_AT = dt.datetime(2026, 9, 15, 6, 30, tzinfo=dt.timezone.utc)


def by_month(report):
    return {(month["employee_code"], month["month"]): month for month in report["months"]}


DAYS = "/api/v1/attendance/days/"
RAW = "/api/v1/attendance/raw/"
RECENT = "/api/v1/attendance/dashboard/recent-punches/"
CORRECTIONS = "/api/v1/attendance/corrections/"


def _queries(client, path, params=None) -> tuple[int, object]:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(path, params or {})
    assert response.status_code == 200, response.content
    return len(captured), response


class TestDaysListQueries:
    def _people_on_their_own_terminals(self, world, first: int, last: int):
        for index in range(first, last):
            office = OfficeFactory(code=f"X{index}", name=f"Extra {index}", timezone="Asia/Kolkata", default_shift=world.shift)
            device = DeviceFactory(name=f"X-{index}", serial_number=f"NCDX{index:09d}", office=office)
            person = EmployeeFactory(code=f"X{index:03d}", full_name=f"Extra {index}", office=office, joined_on=dt.date(2026, 1, 1))
            DeviceUserFactory(device=device, pin="7", employee=person)
            punch(device, "7", at(14, 9, 30))
            punch(device, "7", at(14, 18, 30))
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")

    def test_the_first_terminal_and_its_office_do_not_cost_a_query_per_row(self, hr_client, world):
        self._people_on_their_own_terminals(world, 0, 2)
        _queries(hr_client, DAYS)  # warm the caller's permission cache
        few, _ = _queries(hr_client, DAYS, {"date_from": "2026-09-14", "date_to": "2026-09-14"})
        self._people_on_their_own_terminals(world, 2, 6)
        many, response = _queries(hr_client, DAYS, {"date_from": "2026-09-14", "date_to": "2026-09-14"})
        extra = [row for row in response.json()["results"] if row["employee"]["code"].startswith("X")]
        assert len(extra) == 6 and all(row["first_device"]["office"]["name"] == row["office"]["name"] for row in extra)
        assert many == few

    def test_raw_punches_and_recent_punches_do_not_cost_a_query_per_person(self, hr_client, world):
        self._people_on_their_own_terminals(world, 0, 2)
        _queries(hr_client, RAW)
        few_raw, _ = _queries(hr_client, RAW)
        few_recent, _ = _queries(hr_client, RECENT)
        self._people_on_their_own_terminals(world, 2, 6)
        many_raw, response = _queries(hr_client, RAW)
        offices = {row["employee"]["office"]["name"] for row in response.json()["results"] if row["employee"]["code"].startswith("X")}
        assert offices == {f"Extra {index}" for index in range(6)}
        many_recent, _ = _queries(hr_client, RECENT)
        assert (many_raw, many_recent) == (few_raw, few_recent)

    def test_corrections_do_not_cost_a_query_per_person(self, hr_client, world):
        self._people_on_their_own_terminals(world, 0, 2)

        def correct_everyone():
            for day in AttendanceDay.objects.filter(employee__code__startswith="X", is_corrected=False):
                response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "overtime_minutes", "new": 5, "reason": "x"}, format="json")
                assert response.status_code == 201

        correct_everyone()
        _queries(hr_client, CORRECTIONS)
        few, _ = _queries(hr_client, CORRECTIONS)
        self._people_on_their_own_terminals(world, 2, 6)
        correct_everyone()
        many, response = _queries(hr_client, CORRECTIONS)
        body = response.json()
        assert body["count"] == 6 and {row["employee"]["office"]["name"] for row in body["results"]} == {f"Extra {index}" for index in range(6)}
        assert many == few


class TestEsslHistory:
    """PLAN §7.5: eSSL's attendance history is recomputed by v4 — all of it, for everyone eSSL stored days for."""

    @pytest.fixture
    def tables(self):
        return json.loads((Path(__file__).parent / "legacy" / "essl_attendance_tables.json").read_text())

    def _import(self, tables):
        hr_import.import_all(tables)
        devices_import.import_all(tables, now=IMPORT_AT)
        legacy_import.import_raw_punches(tables["attendance_raw"])
        return legacy_import.status_diff_report(tables["attendance"], at=IMPORT_AT)

    def test_a_history_longer_than_one_recompute_window_is_recomputed_in_full(self, tables, settings):
        settings.ATTENDANCE_MAX_RECOMPUTE_DAYS = 10
        report = self._import(tables)
        assert report["days_differing"] == 5
        assert not [item for month in report["months"] for item in month["differences"] if item["v4_status"] == "NOT_STORED"]

    def test_the_history_of_people_who_already_left_is_kept(self, tables):
        for row in tables["employees"]:
            if row["employee_code"] == "E004":
                row["is_active"] = False  # deactivated in eSSL (no left_on: eSSL never asked)
        report = self._import(tables)
        e004 = Employee.objects.get(code="E004")
        v3_days = {row["work_date"] for row in tables["attendance"] if row["employee_id"] == 4}
        stored = {day.isoformat() for day in AttendanceDay.objects.filter(employee=e004).values_list("work_date", flat=True)}
        assert v3_days and v3_days <= stored
        assert report["days_differing"] == 5 and by_month(report)[("E004", "2026-08")]["days_differing"] == 0


def test_the_database_checks_that_migrate_runs_pass():
    """``manage.py migrate`` runs the database-dependent system checks (index names ≤ 30 characters, models.E034)."""
    from django.apps import apps
    from django.core import checks

    errors = [error for error in checks.run_checks(app_configs=[apps.get_app_config("attendance")], databases=["default"]) if error.level >= checks.ERROR]
    assert errors == []


def test_process_all_records_who_asked(hr_client, world):
    from audit.models import AuditLog

    punch(world.d1, "1", at(14, 9, 30))
    assert hr_client.post("/api/v1/attendance/process-all/").status_code == 202
    row = AuditLog.objects.get(action="attendance.process_all_requested")
    assert row.actor_id == world.hr.pk and row.after["date_from"] == "2026-09-13" and row.after["date_to"] == "2026-09-15"


class TestCorrectionValues:
    @pytest.mark.parametrize("field", ["working_minutes", "overtime_minutes", "late_minutes"])
    def test_minutes_beyond_two_days_are_a_validation_error_never_a_500(self, hr_client, world, field):
        from attendance.tests.factories import AttendanceDayFactory

        day = AttendanceDayFactory(employee=world.binu, work_date=dt.date(2026, 9, 14))
        for value in (10**12, 2 * 24 * 60 + 1):
            response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": field, "new": value, "reason": "typo"}, format="json")
            assert response.status_code == 400 and response.json()["code"] == "validation_error" and "new" in response.json()["errors"]
        assert hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": field, "new": 2 * 24 * 60, "reason": "ok"}, format="json").status_code == 201

    @pytest.mark.parametrize("value", ["2026-13-14T09:00:00", "2026-09-14T25:00:00", 930, True])
    def test_an_impossible_clock_reading_is_a_validation_error(self, hr_client, world, value):
        from attendance.tests.factories import AttendanceDayFactory

        day = AttendanceDayFactory(employee=world.binu, work_date=dt.date(2026, 9, 14))
        response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "first_in", "new": value, "reason": "typo"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and "new" in response.json()["errors"]


class TestExtremeDates:
    @pytest.mark.parametrize("value", ["0001-01-01", "9999-12-31"])
    def test_dates_at_the_ends_of_the_calendar_are_validation_errors_never_a_500(self, hr_client, world, value):
        base = "/api/v1/attendance/"
        requests = [
            ("day/", {"day": value}),
            ("dashboard/summary/", {"day": value}),
            (f"employees/{world.asha.uid}/timeline/", {"work_date": value}),
            ("reports/daily/", {"day": value}),
            ("reports/weekly/", {"week_start": value}),
            ("reports/individual/", {"date_from": value, "date_to": value}),
            ("reports/office/", {"date_from": value, "date_to": value}),
            ("raw/", {"date_from": value, "date_to": value}),
            ("days/", {"date_from": value, "date_to": value}),
        ]
        for path, params in requests:
            response = hr_client.get(base + path, params)
            assert response.status_code in (200, 400), (path, response.status_code)
            if response.status_code == 400:
                assert response.json()["code"] == "validation_error"
        for path in ("process/", "recalculate/"):
            response = hr_client.post(base + path, {"date_from": value, "date_to": value}, format="json")
            assert response.status_code in (200, 400), (path, response.status_code)


class TestDayRosterIsBounded:
    def test_the_day_view_refuses_more_people_than_it_draws(self, hr_client, world, monkeypatch):
        from attendance.services import calendar

        monkeypatch.setattr(calendar, "MAX_DAY_EMPLOYEES", 3)
        response = hr_client.get("/api/v1/attendance/day/", {"day": "2026-09-14"})
        assert response.status_code == 400 and response.json()["code"] == "too_many_employees" and "office" in response.json()["errors"]
        narrowed = hr_client.get("/api/v1/attendance/day/", {"day": "2026-09-14", "office": str(world.br.uid)})
        assert narrowed.status_code == 200 and len(narrowed.json()["rows"]) == 2


class TestReportFileNames:
    @pytest.mark.parametrize("code, stem", [('E"1', "E_1"), ("E\n1", "E_1"), ("E;1 x", "E_1_x"), ("ഇ1", "_1")])
    def test_an_employee_code_never_breaks_the_attachment_header(self, hr_client, world, code, stem):
        world.asha.code = code
        world.asha.save()
        for fmt in ("csv", "xlsx"):
            response = hr_client.get("/api/v1/attendance/reports/monthly-individual/", {"year": 2026, "month": 9, "employee": str(world.asha.uid), "format": fmt})
            assert response.status_code == 200
            assert response["Content-Disposition"] == f'attachment; filename="employee-{stem}-2026-09.{fmt}"'
