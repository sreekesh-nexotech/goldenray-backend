"""The eSSL import (PLAN §7.5) on the masked tables captured from a private eSSL instance (capture_essl.py).

hr and devices import first; then the raw punches (new content keys, collapsed duplicates reported) and the recompute
+ per-employee-month status diff (v3 stored statuses against v4) for HR sign-off.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from attendance.models import AttendanceDay, RawPunch
from attendance.services import legacy_import
from audit.models import AuditLog
from core.models import LegacyMap
from devices.services import legacy_import as devices_import
from hr.models import Employee
from hr.services import legacy_import as hr_import

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "legacy"
AT = dt.datetime(2026, 9, 15, 6, 30, tzinfo=dt.timezone.utc)


@pytest.fixture
def tables():
    return json.loads((FIXTURES / "essl_attendance_tables.json").read_text())


@pytest.fixture
def people(tables):
    hr_import.import_all(tables)
    devices_import.import_all(tables, now=AT)
    return {employee.code: employee for employee in Employee.objects.all()}


def by_code(report):
    return {(month["employee_code"], month["month"]): month for month in report["months"]}


class TestRawPunches:
    def test_every_punch_once_with_its_content_key(self, tables, people):
        report = legacy_import.import_raw_punches(tables["attendance_raw"])
        assert (report["created"], report["updated"], report["violations"]) == (34, 0, [])
        # eSSL stored the 18 August 09:30 punch twice: the ADMS push and the agent's upload of it (§I.22)
        assert [(item["pin"], item["device_time"], item["essl_source"]) for item in report["collapsed"]] == [("1", "2026-08-18T09:30:00", "AGENT_PUSH")]
        assert report["skipped"] == 1 and RawPunch.objects.count() == 34
        row = RawPunch.objects.get(pin="1", device_time=dt.datetime(2026, 8, 3, 9, 35))
        assert row.source == "IMPORT" and row.punch_at == dt.datetime(2026, 8, 3, 4, 5, tzinfo=dt.timezone.utc) and row.raw_payload["_essl"]["source"] == "AGENT_PUSH"
        assert row.device.serial_number == "NCD8253601138" and row.status_code == 15 and row.punch_code == 255
        assert LegacyMap.objects.filter(source_system="ESSL", source_table="attendance_raw").count() == 35
        assert AuditLog.objects.filter(action="attendance.legacy_imported").count() == 1

    def test_a_rerun_creates_nothing_and_later_deliveries_are_duplicates(self, tables, people):
        legacy_import.import_raw_punches(tables["attendance_raw"])
        again = legacy_import.import_raw_punches(tables["attendance_raw"])
        assert (again["created"], again["skipped"], again["collapsed"]) == (0, 35, []) and RawPunch.objects.count() == 34
        # the office agent delivering an old punch after the cutover computes the same key: nothing new
        from devices.models import Device
        from devices.services import ingest

        mars = Device.objects.get(serial_number="NCD8253601138")
        result = ingest.ingest(mars, [{"pin": "1", "device_time": "2026-08-03 09:35:00", "status": 15, "punch": 255}], source=ingest.AGENT_PUSH)
        assert result["new"] == 0 and result["duplicate"] == 1

    def test_unimportable_rows_are_listed(self, tables, people):
        rows = copy.deepcopy(tables["attendance_raw"][:3])
        rows[0]["device_id"] = 999
        rows[1]["device_user_id"] = ""
        rows[2]["status"] = 70000
        report = legacy_import.import_raw_punches(rows)
        assert [(item["source_id"], item["field"]) for item in report["violations"]] == [(str(rows[0]["id"]), "device_id"), (str(rows[1]["id"]), "row"), (str(rows[2]["id"]), "status")]
        assert report["created"] == 1 and RawPunch.objects.get().raw_payload["_essl"]["status"] == 70000 and RawPunch.objects.get().status_code is None


class TestStatusDiff:
    def test_the_recompute_and_the_differences_hr_signs_off(self, tables, people):
        legacy_import.import_raw_punches(tables["attendance_raw"])
        report = legacy_import.status_diff_report(tables["attendance"], at=AT)
        assert report["violations"] == [] and report["skipped"] == 0
        assert report["days_compared"] == 5 * 31
        months = by_code(report)
        asha = {item["date"]: (item["v3_status"], item["v4_status"]) for item in months[("E001", "2026-08")]["differences"]}
        # A4: a worked holiday / Sunday keep their status (v3: LATE / PRESENT from the punches)
        assert asha == {"2026-08-15": ("LATE", "HOLIDAY"), "2026-08-16": ("PRESENT", "WEEKLY_OFF")}
        binu = {item["date"]: (item["v3_status"], item["v4_status"]) for item in months[("E002", "2026-08")]["differences"]}
        assert binu == {"2026-08-03": ("LATE", "HALF_DAY")}  # A5: 11:40 on an 11:00 shift is past start + 30 (v3: 10:00 wall clock)
        chitra = {item["date"]: (item["v3_status"], item["v4_status"]) for item in months[("E003", "2026-08")]["differences"]}
        assert chitra == {"2026-08-05": ("PRESENT", "ABSENT")}  # A6: the 06:45 OUT belongs to the 4th
        elena = {item["date"]: (item["v3_status"], item["v4_status"]) for item in months[("5", "2026-08")]["differences"]}
        assert elena == {"2026-08-03": ("PRESENT", "ABSENT")}  # A1: no employee_code fallback for an unlinked PIN
        assert months[("E004", "2026-08")]["days_differing"] == 0  # PIN 4 stays linked on both terminals (reported by devices)
        assert report["days_differing"] == 5
        day = AttendanceDay.objects.get(employee=people["E001"], work_date=dt.date(2026, 8, 11))
        assert day.status == "PRESENT" and day.leave_conflict  # A4: full-day leave with punches is flagged
        double_scan = AttendanceDay.objects.get(employee=people["E001"], work_date=dt.date(2026, 8, 6))
        assert (double_scan.punch_count, len(double_scan.ignored_raw_ids), double_scan.missing_out) == (1, 1, True)  # A3
        short = AttendanceDay.objects.get(employee=people["E001"], work_date=dt.date(2026, 8, 7))
        assert short.working_minutes == 61  # A2 (v3: 1)
        duplicate = AttendanceDay.objects.get(employee=people["E001"], work_date=dt.date(2026, 8, 18))
        assert duplicate.punch_count == 2  # v3 counted the stored duplicate: 3
        totals = legacy_import.month_totals(report)
        assert totals["HOLIDAY"] == {"v3": 4, "v4": 5} and totals["WEEKLY_OFF"]["v4"] == totals["WEEKLY_OFF"]["v3"] + 1

    def test_unmapped_employees_are_listed_and_a_rerun_changes_nothing(self, tables, people):
        legacy_import.import_raw_punches(tables["attendance_raw"])
        rows = copy.deepcopy(tables["attendance"])
        rows.append({**rows[0], "id": 99999, "employee_id": 999})
        rows[1]["is_manual_override"] = True
        report = legacy_import.status_diff_report(rows, at=AT)
        assert {item["field"] for item in report["violations"]} == {"employee_id", "is_manual_override"} and report["skipped"] == 1
        again = legacy_import.status_diff_report(tables["attendance"], at=AT)
        assert (again["created"], again["updated"]) == (0, 0) and again["days_differing"] == report["days_differing"]

    def test_import_all_and_an_empty_diff(self, tables, people):
        result = legacy_import.import_all(tables, at=AT)
        assert result["attendance_raw"]["created"] == 34 and result["attendance"]["days_differing"] == 5
        assert legacy_import.status_diff_report([], at=AT)["months"] == []
