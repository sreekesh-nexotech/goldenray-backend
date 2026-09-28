"""eSSL import on fixtures exported from a real eSSL database (hr/tests/legacy/capture_essl.py): mapping rules,
violations, idempotency, bcrypt passwords that keep working and are upgraded to Argon2 on login."""

import copy
import json
from pathlib import Path

import pytest

from accounts.models import User
from audit.models import AuditLog
from core.models import LegacyMap
from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, LeaveType, Office, Shift
from hr.services import legacy_import
from hr.tests.legacy.capture_essl import PASSWORDS

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).resolve().parent / "legacy"
LOGIN = "/api/v1/auth/login/"


@pytest.fixture
def tables():
    return json.loads((FIXTURES / "essl_tables.json").read_text())


@pytest.fixture
def imported(tables):
    return legacy_import.import_all(tables)


def violations(report, field=None):
    return [row for row in report["violations"] if field is None or row["field"] == field]


def source_row(tables, table, **match):
    return next(row for row in tables[table] if all(row[key] == value for key, value in match.items()))


def by_source(model, table, source_id):
    return legacy_import.mapped(model, table, source_id)


class TestCounts:
    def test_every_table(self, imported, tables):
        counts = {name: (report["created"], report["updated"], report["skipped"]) for name, report in imported.items()}
        assert counts == {
            "users": (7, 0, 0),
            "shifts": (6, 0, 0),
            "offices": (7, 0, 0),
            "employees": (9, 0, 0),
            "holidays": (5, 0, 0),  # the duplicate global date eSSL's PATCH let through is refused
            "leave_types": (4, 0, 1),  # "sick" merged into "Sick"
            "leave_records": (8, 0, 0),
            "attendance_rules": (6, 0, 0),
        }
        assert LegacyMap.objects.filter(source_system="ESSL").count() == 7 + 6 + 7 + 9 + 5 + 5 + 8 + 6
        assert AuditLog.objects.filter(action="hr.legacy_imported").count() == 8

    def test_rerun_changes_nothing(self, imported, tables):
        again = legacy_import.import_all(tables)
        assert {name: (report["created"], report["updated"]) for name, report in again.items()} == {name: (0, 0) for name in again}
        assert LegacyMap.objects.filter(source_system="ESSL").count() == 53
        assert Employee.objects.count() == 9 and User.objects.count() == 7

    def test_rerun_updates_changed_rows_and_keeps_deleted_ones_deleted(self, imported, tables):
        changed = copy.deepcopy(tables)
        source_row(changed, "employees", employee_code="E004")["designation"] = "Senior Installer"
        report = legacy_import.import_employees(changed["employees"])
        assert (report["created"], report["updated"]) == (0, 1)
        employee = by_source(Employee, "employees", source_row(tables, "employees", employee_code="E004")["id"])
        assert employee.designation == "Senior Installer" and employee.version == 2
        employee.soft_delete()
        source_row(changed, "employees", employee_code="E004")["designation"] = "Lead"
        assert legacy_import.import_employees(changed["employees"])["updated"] == 0
        assert Employee.all_objects.get(pk=employee.pk).designation == "Senior Installer"


class TestUsers:
    def test_role_mapping_and_emails(self, imported, tables):
        users = {row["username"]: by_source(User, "users", row["id"]) for row in tables["users"]}
        assert {name: account.role.slug for name, account in users.items()} == {
            "admin": "admin",
            "asha": "staff",
            "binu": "hr",
            "chitra": "staff",  # VIEWER
            "elena": "admin",
            "farhan": "staff",
            "gita": "staff",
        }
        assert users["admin"].email == "admin@migrated.invalid" and users["elena"].email == "elena@migrated.invalid"
        assert {row["source_id"] for row in violations(imported["users"], "email")} == {str(source_row(tables, "users", username=name)["id"]) for name in ("admin", "elena")}
        assert users["asha"].email == source_row(tables, "users", username="asha")["email"]
        assert users["farhan"].is_active is False and users["asha"].must_reset_password is False
        assert all(account.password.startswith("bcrypt$$2b$") for account in users.values())

    def test_old_passwords_keep_working_and_are_upgraded(self, imported, tables, api_client):
        email = source_row(tables, "users", username="asha")["email"]
        assert api_client.post(LOGIN, {"email": email, "password": "wrong-password-1"}, format="json").status_code == 401
        response = api_client.post(LOGIN, {"email": email, "password": PASSWORDS["asha"]}, format="json")
        assert response.status_code == 200, response.json()
        assert User.objects.get(email=email).password.startswith("argon2$")
        # the upgraded hash survives a re-run of the import
        legacy_import.import_users(tables["users"], roles=tables["roles"])
        assert User.objects.get(email=email).password.startswith("argon2$")
        assert api_client.post(LOGIN, {"email": email, "password": PASSWORDS["asha"]}, format="json").status_code == 200

    def test_passwords_longer_than_72_bytes_work_like_in_essl(self, imported, tables, api_client):
        email = source_row(tables, "users", username="gita")["email"]
        assert len(PASSWORDS["gita"]) == 80
        assert api_client.post(LOGIN, {"email": email, "password": PASSWORDS["gita"]}, format="json").status_code == 200
        assert api_client.post(LOGIN, {"email": email, "password": PASSWORDS["gita"][:72] + "different"}, format="json").status_code == 401

    def test_non_bcrypt_hash_needs_a_reset(self, tables):
        rows = [dict(tables["users"][1], id=999, username="odd", email="odd@example.com", password_hash="plain-text")]
        report = legacy_import.import_users(rows, roles=tables["roles"])
        account = User.objects.get(email="odd@example.com")
        assert not account.has_usable_password() and account.must_reset_password and violations(report, "password_hash")
        assert legacy_import.import_users(rows, roles=tables["roles"])["updated"] == 0

    def test_existing_platform_account_is_linked_not_overwritten(self, tables, make_user):
        row = source_row(tables, "users", username="binu")
        existing = make_user(email=row["email"], grants={"blogs": ["view"]})
        report = legacy_import.import_users([row], roles=tables["roles"])
        assert report["skipped"] == 1 and "already belongs" in violations(report, "email")[0]["message"]
        existing.refresh_from_db()
        assert existing.role.permissions == {"blogs": ["view"]} and by_source(User, "users", row["id"]) == existing
        password = existing.password
        again = legacy_import.import_users([row], roles=tables["roles"])  # a re-run does not overwrite it either
        existing.refresh_from_db()
        assert (again["updated"], again["skipped"]) == (0, 1) and existing.password == password and existing.role.permissions == {"blogs": ["view"]}

    def test_unknown_role(self, tables):
        row = dict(source_row(tables, "users", username="asha"), id=998, email="x@example.com", role_id=None)
        report = legacy_import.import_users([row], roles=tables["roles"])
        assert User.objects.get(email="x@example.com").role.slug == "staff" and violations(report, "role_id")


class TestOfficesAndShifts:
    def test_offices(self, imported, tables):
        office = {row["code"]: by_source(Office, "offices", row["id"]) for row in tables["offices"]}
        assert office["WFH"].timezone == "Asia/Kolkata" and violations(imported["offices"], "timezone")[0]["source_id"] == str(source_row(tables, "offices", code="WFH")["id"])
        assert office["LAB"].timezone == "Asia/Calcutta" and office["DXB"].timezone == "Asia/Dubai"
        assert office["BR2"].is_active is False and office["SALES"].default_shift.code == "EARLY" and office["HO"].default_shift.code == "GEN"
        assert office["SALES"].address == "2nd Floor, MG Road, Kochi" and office["HO"].address == ""

    def test_shifts(self, imported, tables):
        shift = {row["code"]: by_source(Shift, "shifts", row["id"]) for row in tables["shifts"]}
        assert shift["LATE"].is_overnight is True and any(row["field"] == "is_overnight" for row in imported["shifts"]["violations"])
        assert shift["NIGHT"].is_overnight is True and shift["OLD"].is_active is False
        assert shift["EARLY"].weekly_off_days == [5, 6] and shift["EARLY"].overtime_enabled is False and shift["EARLY"].full_day_minutes == 450
        assert (shift["GEN"].overnight_buffer_minutes, shift["GEN"].half_day_after_minutes, shift["GEN"].debounce_minutes) == (180, 30, 2)
        deadline_notes = {row["source_id"] for row in violations(imported["shifts"], "half_day_after_minutes")}
        assert str(source_row(tables, "shifts", code="GEN")["id"]) not in deadline_notes  # 09:30 + 30 = 10:00, as in eSSL
        assert str(source_row(tables, "shifts", code="EARLY")["id"]) in deadline_notes
        assert violations(imported["shifts"], "office_id")[0]["source_id"] == str(source_row(tables, "shifts", code="HALFSAT")["id"])


class TestEmployees:
    def test_mapping(self, imported, tables):
        employee = {row["employee_code"]: by_source(Employee, "employees", row["id"]) for row in tables["employees"]}
        asha = employee["E001"]
        assert asha.office.code == "HO" and asha.shift.code == "GEN" and asha.identity_method == "FACE" and asha.phone_e164.startswith("+91")
        assert asha.user.email == source_row(tables, "users", username="asha")["email"] and asha.joined_on.isoformat() == "2023-04-03"
        assert employee["E002"].shift is None and employee["E002"].effective_shift.code == "GEN"
        assert employee["E003"].phone_e164.startswith("+91484") and employee["E003"].identity_method == "CARD"  # a Kochi landline
        assert employee["E004"].phone_e164 == "" and employee["E008"].email == "" and employee["E008"].office is None
        assert employee["E006"].is_active is False and employee["E006"].user is None  # eSSL revoked the login
        assert employee["e007"].is_active is False and employee["e007"].user.is_active is True
        fields = {(row["source_id"], row["field"]) for row in imported["employees"]["violations"]}
        ids = {row["employee_code"]: str(row["id"]) for row in tables["employees"]}
        assert {(ids["E004"], "phone"), (ids["E008"], "email"), (ids["e007"], "is_active")} <= fields

    def test_a_login_linked_elsewhere_is_not_linked_twice(self, imported, tables):
        rows = [dict(source_row(tables, "employees", employee_code="E009"), user_id=source_row(tables, "users", username="asha")["id"])]
        report = legacy_import.import_employees(rows)
        assert violations(report, "user_id") and by_source(Employee, "employees", rows[0]["id"]).user is None

    def test_missing_references_are_reported(self, tables):
        report = legacy_import.import_employees([dict(tables["employees"][0], office_id=777, shift_id=778, user_id=779)])
        assert {row["field"] for row in report["violations"]} == {"office_id", "shift_id", "user_id"}
        assert report["created"] == 1


class TestCalendar:
    def test_holidays(self, imported, tables):
        assert Holiday.objects.count() == 5 and Holiday.objects.filter(office__isnull=True).count() == 3
        [skipped] = violations(imported["holidays"], "holiday_date")
        assert skipped["source_id"] == str(source_row(tables, "holidays", name="Gandhi Jayanti (typo)")["id"])
        assert Holiday.objects.get(name="Thiruvonam").notes == "State holiday" and Holiday.objects.get(name="May Day").is_active is False

    def test_leave_types_and_records(self, imported, tables):
        assert sorted(LeaveType.objects.values_list("code", flat=True)) == ["CASUAL_LEAVE", "LEAVE", "ON_DUTY", "SICK"]
        assert LeaveType.objects.get(code="SICK").name == "Sick"
        assert [row["source_id"] for row in violations(imported["leave_types"], "leave_type")] == ["sick"]
        assert LeaveRecord.objects.count() == 8
        statuses = sorted(LeaveRecord.objects.values_list("status", flat=True))
        assert statuses == sorted(row["status"] for row in tables["leave_records"])
        gita = by_source(LeaveRecord, "leave_records", source_row(tables, "leave_records", employee_id=source_row(tables, "employees", employee_code="e007")["id"])["id"])
        assert gita.is_half_day is False and violations(imported["leave_records"], "is_half_day")
        assert violations(imported["leave_records"], "date_from")  # the overlap eSSL allowed
        cancelled = LeaveRecord.objects.get(status="CANCELLED")
        assert cancelled.leave_type.code == "SICK" and cancelled.decided_at is not None
        assert LeaveRecord.objects.get(status="PENDING").decided_at is None

    def test_leave_needs_its_employee_and_type(self, tables):
        report = legacy_import.import_leave_records(tables["leave_records"][:1])
        assert report["created"] == 0 and violations(report, "employee_id")

    def test_attendance_rules(self, imported, tables):
        rule = {row["name"]: by_source(AttendanceRule, "attendance_rules", row["id"]) for row in tables["attendance_rules"]}
        assert rule["Company half-day deadline"].scope == "GLOBAL" and rule["Company half-day deadline"].rules == {"half_day_after": "10:30"}
        assert rule["Head office allowance"].rules == {"half_day_after_minutes": 45} and rule["Head office allowance"].office.code == "HO"
        assert rule["Sales early shift"].scope == "SHIFT" and rule["Sales early shift"].office is None and rule["Sales early shift"].rules == {"half_day_after": "08:15"}
        assert rule["Broken deadline"].rules == {} and rule["Future policy"].is_active is False
        fields = {row["field"] for row in imported["attendance_rules"]["violations"]}
        assert {"rules.grace_bonus", "rules.half_day_after", "rules.half_day_under_minutes", "office_id"} <= fields
