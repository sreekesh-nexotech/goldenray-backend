"""Monthly partitions of audit_log and the ensure_audit_partitions command."""

import uuid
from datetime import date, datetime
from datetime import timezone as dt_timezone
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from audit.models import AuditLog
from audit.services import partitions, record
from audit.services.partitions import MonthRange, add_months, ensure_partitions, existing_partitions, month_ranges, month_start, privileges_of, table_owner

pytestmark = pytest.mark.django_db


def _partition_of(entry) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT tableoid::regclass::text FROM audit_log WHERE id = %s AND at = %s", [entry.id, entry.at])
        return cursor.fetchone()[0]


def _temporary_role() -> str:
    name = f"flarize_test_app_{uuid.uuid4().hex[:10]}"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {connection.ops.quote_name(name)} NOLOGIN")
    return name


class TestHelpers:
    def test_month_arithmetic(self):
        assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
        assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
        assert month_start(date(2026, 9, 28)) == date(2026, 9, 1)
        assert month_start(datetime(2026, 9, 30, 20, 0, tzinfo=dt_timezone.utc)) == date(2026, 9, 1)
        assert month_start(datetime(2026, 10, 1, 1, 0)) == date(2026, 10, 1)
        assert [month.name for month in month_ranges(date(2026, 12, 15), 3)] == ["audit_log_y2026m12", "audit_log_y2027m01", "audit_log_y2027m02"]
        month = MonthRange(date(2026, 12, 1))
        assert (month.lower, month.upper) == ("'2026-12-01 00:00:00+00'", "'2027-01-01 00:00:00+00'")

    def test_months_must_be_positive(self):
        with pytest.raises(ValueError):
            ensure_partitions(0)


def test_the_migration_created_the_current_window():
    names = existing_partitions()
    current = month_start(timezone.now())
    assert "audit_log_default" in names
    assert {month.name for month in month_ranges(current, 3)} <= names
    assert _partition_of(record("tests.now")) == MonthRange(current).name


def test_ensure_creates_future_months_idempotently():
    results = ensure_partitions(2, today=date(2031, 1, 15))
    assert [(result.name, result.created, result.moved_rows) for result in results] == [("audit_log_y2031m01", True, 0), ("audit_log_y2031m02", True, 0)]
    assert [result.created for result in ensure_partitions(2, today=date(2031, 1, 15))] == [False, False]
    future = AuditLog(at=datetime(2031, 2, 3, tzinfo=dt_timezone.utc), action="tests.future")
    future.save()
    assert _partition_of(future) == "audit_log_y2031m02"
    assert AuditLog.objects.get(pk=(future.id, future.at)).action == "tests.future"  # composite primary key lookup


def test_rows_that_landed_in_the_default_partition_are_moved(make_user):
    stranded = AuditLog(at=datetime(2032, 3, 10, 12, 0, tzinfo=dt_timezone.utc), action="tests.stranded", actor=make_user(), actor_kind="USER")
    stranded.save()
    assert _partition_of(stranded) == "audit_log_default"
    result = ensure_partitions(1, today=date(2032, 3, 1))[0]
    assert (result.name, result.created, result.moved_rows) == ("audit_log_y2032m03", True, 1)
    assert _partition_of(stranded) == "audit_log_y2032m03"
    moved = AuditLog.objects.get(action="tests.stranded")
    assert moved.id == stranded.id and moved.actor_id == stranded.actor_id
    # indexes follow the partition: an index scan on the object index is possible on the new partition
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_indexes WHERE tablename = 'audit_log_y2032m03'")
        assert cursor.fetchone()[0] == 4  # pkey + three query indexes


def test_new_partitions_are_closed_to_the_app_role():
    role = _temporary_role()
    partitions.apply_append_only_privileges(role)
    ensure_partitions(1, today=date(2033, 5, 1), app_role=role)
    with connection.cursor() as cursor:
        cursor.execute("SELECT has_table_privilege(%s, 'audit_log_y2033m05', 'UPDATE')", [role])
        assert cursor.fetchone()[0] is False


class TestCommand:
    def test_reports_existing_and_created(self):
        out = StringIO()
        call_command("ensure_audit_partitions", "--months", "4", "--app-role", "", stdout=out)
        text = out.getvalue()
        current = month_start(timezone.now())
        assert f"exists  {MonthRange(current).name}" in text
        assert f"created {MonthRange(add_months(current, 3)).name}" in text

    def test_applies_the_app_role_revoke(self):
        role = _temporary_role()
        out = StringIO()
        call_command("ensure_audit_partitions", "--app-role", role, stdout=out)
        assert f"append-only for role {role!r}" in out.getvalue()
        assert privileges_of(role)["DELETE"] is False

    def test_owner_as_app_role_is_left_alone(self):
        out = StringIO()
        call_command("ensure_audit_partitions", "--app-role", table_owner(), stdout=out)
        assert "single-role setup" in out.getvalue()

    def test_errors(self):
        with pytest.raises(CommandError, match="between 1 and 36"):
            call_command("ensure_audit_partitions", "--months", "0", stdout=StringIO())
        with pytest.raises(CommandError, match="does not exist"):
            call_command("ensure_audit_partitions", "--app-role", "flarize_missing_role", stdout=StringIO())
