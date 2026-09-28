"""audit_log is append-only: the application never updates or deletes, and the REVOKE blocks a separate app role."""

import importlib
import uuid
from types import SimpleNamespace

import pytest
from django.db import ProgrammingError, connection, transaction
from django.test import override_settings

from audit.models import AppendOnlyError, AuditLog
from audit.services import record
from audit.services.partitions import PrivilegeError, apply_append_only_privileges, check_app_role, privileges_of, table_owner

pytestmark = pytest.mark.django_db


class TestApplication:
    def test_queryset_refuses_update_and_delete(self):
        record("tests.thing")
        for attempt in (
            lambda: AuditLog.objects.update(note="x"),
            lambda: AuditLog.objects.filter(action="tests.thing").delete(),
            lambda: AuditLog.objects.all()._raw_delete(connection.alias),
            lambda: AuditLog.objects.bulk_update(list(AuditLog.objects.all()), ["note"]),
            lambda: AuditLog.objects.update_or_create(action="tests.thing", defaults={"note": "x"}),
        ):
            with pytest.raises(AppendOnlyError):
                attempt()

    def test_instances_refuse_save_and_delete(self):
        entry = record("tests.thing")
        entry.note = "rewritten"
        with pytest.raises(AppendOnlyError):
            entry.save()
        with pytest.raises(AppendOnlyError):
            entry.delete()
        assert AuditLog.objects.get(id=entry.id).note == ""


def _temporary_role(**options) -> str:
    """Roles are cluster-wide, but CREATE ROLE is transactional: the test transaction's rollback removes it."""
    name = f"flarize_test_app_{uuid.uuid4().hex[:10]}"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {connection.ops.quote_name(name)} NOLOGIN {options.get('extra', '')}")
    return name


def _as_role(role: str, sql: str):
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {connection.ops.quote_name(role)}")
        try:
            with transaction.atomic():
                cursor.execute(sql)
                return cursor.fetchall() if cursor.description else None
        finally:
            cursor.execute("RESET ROLE")


class TestDatabasePrivileges:
    def test_revoke_blocks_update_delete_and_truncate_for_the_app_role(self):
        role = _temporary_role()
        closed = apply_append_only_privileges(role)
        assert "audit_log_default" in closed
        assert privileges_of(role) == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}

        inserted = _as_role(role, "INSERT INTO audit_log (action, actor_kind) VALUES ('tests.from_app_role', 'SYSTEM') RETURNING id")
        assert inserted and inserted[0][0]
        assert _as_role(role, "SELECT count(*) FROM audit_log WHERE action = 'tests.from_app_role'") == [(1,)]
        for statement in (
            "UPDATE audit_log SET note = 'tampered'",
            "DELETE FROM audit_log",
            "TRUNCATE audit_log",
            "UPDATE audit_log_default SET note = 'tampered'",  # partitions are closed too
            "SELECT count(*) FROM audit_log_default",
        ):
            with pytest.raises(ProgrammingError, match="permission denied"):
                _as_role(role, statement)
        assert AuditLog.objects.get(action="tests.from_app_role").note == ""

    def test_owner_retains_full_control(self):
        """The REVOKE is scoped to the app role; the owner (maintenance, retention) is unaffected."""
        role = _temporary_role()
        apply_append_only_privileges(role)
        assert privileges_of(table_owner())["DELETE"] is True

    def test_unusable_roles_fail_closed(self):
        with pytest.raises(PrivilegeError, match="does not exist"):
            check_app_role("flarize_no_such_role")
        with pytest.raises(PrivilegeError, match="owns"):
            check_app_role(table_owner())
        member = _temporary_role()
        with connection.cursor() as cursor:
            cursor.execute(f"GRANT {connection.ops.quote_name(table_owner())} TO {connection.ops.quote_name(member)}")
        with pytest.raises(PrivilegeError, match="inherits"):
            check_app_role(member)
        with pytest.raises(PrivilegeError, match="inherits"):
            check_app_role(_temporary_role(extra="SUPERUSER"))


class TestMigrationStep:
    migration = importlib.import_module("audit.migrations.0001_initial")

    def _run(self):
        self.migration.apply_append_only(None, SimpleNamespace(connection=connection))

    def test_applies_the_revoke_when_the_app_role_differs_from_the_owner(self):
        role = _temporary_role()
        with override_settings(DB_APP_ROLE=role):
            self._run()
        assert privileges_of(role)["UPDATE"] is False and privileges_of(role)["INSERT"] is True

    @pytest.mark.parametrize("configured", ["", "   "])
    def test_no_app_role_is_a_no_op(self, configured):
        with override_settings(DB_APP_ROLE=configured):
            self._run()

    def test_same_role_as_the_owner_is_a_no_op(self):
        with override_settings(DB_APP_ROLE=table_owner()):
            self._run()
        assert privileges_of(table_owner())["UPDATE"] is True
