"""attendance_raw_punch: the store behind devices' ingestion (DV-78), content dedup (A11), two clocks (A10),
append-only (application + REVOKE), monthly partitions."""

from __future__ import annotations

import datetime as dt
import importlib
import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.test import override_settings

from attendance.models import AppendOnlyError, RawPunch
from attendance.services import partitions, sink
from attendance.tests.conftest import at, events
from attendance.tests.factories import punch
from devices.services import ingest, punch_sink

pytestmark = pytest.mark.django_db


class TestTheSink:
    def test_the_app_installs_the_store(self):
        assert punch_sink.installed() and isinstance(punch_sink.get(), sink.RawPunchSink)

    def test_an_agent_upload_is_stored_once_with_both_clocks(self, world):
        records = [{"pin": "1", "device_time": "2026-09-14 09:31:05", "status": 15, "punch": 255, "device_record_uid": 7, "raw_payload": {"x": 1}}]
        result = ingest.ingest(world.d1, records, source=ingest.AGENT_PUSH)
        assert result == {"received": 1, "new": 1, "duplicate": 0, "invalid": 0, "discarded": 0}
        row = RawPunch.objects.get()
        assert row.device_time == dt.datetime(2026, 9, 14, 9, 31, 5) and row.device_time.tzinfo is None
        assert row.punch_at == dt.datetime(2026, 9, 14, 9, 31, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert (row.pin, row.status_code, row.punch_code, row.device_record_uid, row.source, row.raw_payload) == ("1", 15, 255, 7, "AGENT_PUSH", {"x": 1})
        assert len(row.dedup_key) == 64
        # the same punch again (a resend) and the same content over ADMS: duplicates, never a second row (A11)
        again = ingest.ingest(world.d1, records, source=ingest.AGENT_PUSH)
        adms = ingest.ingest(world.d1, [{**records[0], "device_record_uid": None}], source=ingest.ADMS_PUSH)
        assert again["new"] == adms["new"] == 0 and again["duplicate"] == adms["duplicate"] == 1
        assert RawPunch.objects.count() == 1
        assert [event["new"] for event in events("attendance.punches_ingested")] == [1]

    def test_punch_at_uses_the_terminals_office_zone(self, world):
        row = punch(world.d2, "1", at(14, 9, 0))
        assert row.punch_at == dt.datetime(2026, 9, 14, 5, 0, tzinfo=dt.timezone.utc)  # 09:00 in Dubai (+04:00)

    def test_status_activity_and_observed_codes(self, world):
        punch(world.d1, "1", at(14, 9, 0), status=15, punch_code=255, record_uid=3)
        punch(world.d1, "1", at(14, 18, 0), status=15, punch_code=0, record_uid=4)
        punch(world.d1, "2", at(14, 9, 5), status=4, punch_code=255, record_uid=5)
        store = sink.RawPunchSink()
        assert store.status(world.d1) == {"stored_records": 3, "highest_device_record_uid": 5, "latest_device_time": at(14, 18, 0)}
        assert store.status(world.d2)["stored_records"] == 0
        assert store.pin_activity(world.d1) == [
            {"pin": "1", "punch_count": 2, "first_punch_at": at(14, 9, 0), "last_punch_at": at(14, 18, 0)},
            {"pin": "2", "punch_count": 1, "first_punch_at": at(14, 9, 5), "last_punch_at": at(14, 9, 5)},
        ]
        codes = {(item["field"], item["raw_value"]): item["count"] for item in store.observed_codes()}
        assert codes == {("status", 15): 2, ("status", 4): 1, ("punch", 255): 2, ("punch", 0): 1}

    def test_empty_batches_and_an_unknown_source(self, world):
        assert sink.RawPunchSink().store([]).received == 0
        row = punch(world.d1, "9", at(14, 8, 0), source="SOMETHING")
        assert row.source == "IMPORT"

    def test_the_database_refuses_a_second_row_for_a_key(self, world):
        row = punch(world.d1, "1", at(14, 9, 0))
        with pytest.raises(IntegrityError), transaction.atomic():
            RawPunch.objects.create(device=world.d1, pin="1", device_time=row.device_time, punch_at=row.punch_at, source="IMPORT", dedup_key=row.dedup_key)


class TestAppendOnly:
    def test_the_application_never_changes_a_punch(self, world):
        row = punch(world.d1, "1", at(14, 9, 0))
        row.pin = "2"
        for attempt in (row.save, row.delete, lambda: RawPunch.objects.update(pin="x"), lambda: RawPunch.objects.filter(pin="1").delete()):
            with pytest.raises(AppendOnlyError):
                attempt()
        assert RawPunch.objects.get().pin == "1"


def _temporary_role() -> str:
    name = f"flarize_test_att_{uuid.uuid4().hex[:10]}"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {connection.ops.quote_name(name)} NOLOGIN")
    return name


def _as_role(role: str, sql: str, params=None):
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {connection.ops.quote_name(role)}")
        try:
            with transaction.atomic():
                cursor.execute(sql, params)
                return cursor.fetchall() if cursor.description else None
        finally:
            cursor.execute("RESET ROLE")


class TestDatabasePrivileges:
    def test_revoke_blocks_update_delete_and_truncate_and_closes_the_partitions(self, world):
        punch(world.d1, "1", at(14, 9, 0))
        role = _temporary_role()
        closed = partitions.apply_append_only_privileges(role)
        assert partitions.DEFAULT_PARTITION in closed
        assert partitions.privileges_of(role) == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
        insert = (
            "INSERT INTO attendance_raw_punch (device_id, pin, device_time, punch_at, source, dedup_key) "
            "VALUES (%s, '5', '2026-09-14 10:00:00', '2026-09-14 04:30:00+00', 'IMPORT', %s) RETURNING id"
        )
        assert _as_role(role, insert, [world.d1.pk, "a" * 64])[0][0]
        assert _as_role(role, "SELECT count(*) FROM attendance_raw_punch") == [(2,)]
        for statement in ("UPDATE attendance_raw_punch SET pin = 'x'", "DELETE FROM attendance_raw_punch", "TRUNCATE attendance_raw_punch", f"SELECT * FROM {partitions.DEFAULT_PARTITION}"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                _as_role(role, statement)
        assert RawPunch.objects.filter(pin="x").count() == 0

    def test_unusable_roles_fail_closed(self):
        with pytest.raises(partitions.PrivilegeError, match="does not exist"):
            partitions.check_app_role("flarize_no_such_role")
        with pytest.raises(partitions.PrivilegeError, match="owns"):
            partitions.check_app_role(partitions.table_owner())
        member = _temporary_role()
        with connection.cursor() as cursor:
            cursor.execute(f"GRANT {connection.ops.quote_name(partitions.table_owner())} TO {connection.ops.quote_name(member)}")
        with pytest.raises(partitions.PrivilegeError, match="inherits"):
            partitions.check_app_role(member)


class TestPartitions:
    def test_monthly_partitions_on_the_terminal_clock(self):
        assert partitions.connected_as_owner()
        created = partitions.ensure_partitions(2, today=dt.date(2031, 1, 20))
        assert [item["name"] for item in created] == ["attendance_raw_punch_y2031m01", "attendance_raw_punch_y2031m02"]
        assert partitions.ensure_partitions(2, today=dt.date(2031, 1, 20)) == []  # idempotent
        assert partitions.missing_upcoming(2, today=dt.date(2031, 1, 1)) == []
        assert partitions.missing_upcoming(2, today=dt.date(2031, 2, 1)) == ["attendance_raw_punch_y2031m03"]
        with pytest.raises(ValueError):
            partitions.ensure_partitions(0)

    def test_rows_stranded_in_the_default_partition_move_into_their_month(self, world):
        row = punch(world.d1, "1", dt.datetime(2019, 5, 3, 9, 0))  # no 2019 partition: the DEFAULT one holds it
        assert partitions.default_partition_rows() == 1
        moved = partitions.ensure_months([dt.date(2019, 5, 17)])
        assert moved == [{"name": "attendance_raw_punch_y2019m05", "moved_rows": 1}]
        assert partitions.default_partition_rows() == 0
        assert RawPunch.objects.get().dedup_key == row.dedup_key

    def test_maintain_reports_the_owner_and_what_it_created(self):
        result = partitions.maintain(3)
        assert result["owner"] is True and isinstance(result["created"], list) and result["default_partition_rows"] == 0


class TestMigrationStepAndCommand:
    migration = importlib.import_module("attendance.migrations.0001_initial")

    def test_the_migration_applies_the_revoke_for_a_separate_app_role(self):
        role = _temporary_role()
        with override_settings(DB_APP_ROLE=role):
            self.migration.apply_append_only(None, SimpleNamespace(connection=connection))
        assert partitions.privileges_of(role)["UPDATE"] is False and partitions.privileges_of(role)["INSERT"] is True

    @pytest.mark.parametrize("configured", ["", "  "])
    def test_no_app_role_or_the_owner_is_a_no_op(self, configured):
        with override_settings(DB_APP_ROLE=configured):
            self.migration.apply_append_only(None, SimpleNamespace(connection=connection))
        with override_settings(DB_APP_ROLE=partitions.table_owner()):
            self.migration.apply_append_only(None, SimpleNamespace(connection=connection))
        assert partitions.privileges_of(partitions.table_owner())["UPDATE"] is True

    def test_the_command_is_idempotent(self):
        role = _temporary_role()
        for _ in range(2):
            out = io.StringIO()
            call_command("maintain_attendance_punches", "--app-role", role, stdout=out)
            assert "append-only" in out.getvalue()
        assert partitions.privileges_of(role)["DELETE"] is False

    def test_the_command_in_single_role_setups_and_with_bad_input(self):
        out = io.StringIO()
        with override_settings(DB_APP_ROLE=""):
            call_command("maintain_attendance_punches", stdout=out)
        call_command("maintain_attendance_punches", "--app-role", partitions.table_owner(), stdout=out)
        assert "left unchanged" in out.getvalue()
        with pytest.raises(CommandError, match="does not exist"):
            call_command("maintain_attendance_punches", "--app-role", "flarize_no_such_role", stdout=out)
        with pytest.raises(CommandError, match="at least 1"):
            call_command("maintain_attendance_punches", "--months", "0", stdout=out)

    def test_every_deploy_runs_it_after_migrate(self):
        script = (Path(settings.BASE_DIR) / "deploy" / "release.sh").read_text()
        assert script.index("manage.py migrate --noinput") < script.index("manage.py maintain_attendance_punches")

    def test_the_beat_task_maintains_partitions(self):
        from attendance.tasks import maintain_punch_partitions

        assert maintain_punch_partitions.run(2)["owner"] is True
