"""inventory_movement is append-only: the application never updates or deletes, and the REVOKE blocks a separate app role."""

import importlib
import io
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import ProgrammingError, connection, transaction
from django.test import override_settings

from inventory.models import AppendOnlyError, Movement
from inventory.services.privileges import PrivilegeError, apply_append_only_privileges, check_app_role, privileges_of, table_owner
from inventory.tests.factories import MovementFactory

pytestmark = pytest.mark.django_db


class TestApplication:
    def test_queryset_refuses_update_and_delete(self):
        MovementFactory()
        for attempt in (
            lambda: Movement.objects.update(note="x"),
            lambda: Movement.all_objects.filter(reason="PURCHASE").delete(),
            lambda: Movement.objects.all()._raw_delete(connection.alias),
            lambda: Movement.objects.bulk_update(list(Movement.objects.all()), ["note"]),
            lambda: Movement.objects.update_or_create(reason="PURCHASE", defaults={"note": "x"}),
        ):
            with pytest.raises(AppendOnlyError):
                attempt()

    def test_instances_refuse_save_delete_and_soft_delete(self):
        movement = MovementFactory(note="")
        movement.note = "rewritten"
        for attempt in (movement.save, movement.delete, movement.soft_delete, movement.restore, lambda: movement.versioned_update(None, note="x")):
            with pytest.raises(AppendOnlyError):
                attempt()
        assert Movement.objects.get(pk=movement.pk).note == ""


def _temporary_role(**options) -> str:
    """Roles are cluster-wide, but CREATE ROLE is transactional: the test transaction's rollback removes it."""
    name = f"flarize_test_inv_{uuid.uuid4().hex[:10]}"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {connection.ops.quote_name(name)} NOLOGIN {options.get('extra', '')}")
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
    def test_revoke_blocks_update_delete_and_truncate_for_the_app_role(self):
        movement = MovementFactory()
        role = _temporary_role()
        apply_append_only_privileges(role)
        assert privileges_of(role) == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}
        assert privileges_of(role, "inventory_balance")["SELECT"] is True

        insert = (
            "INSERT INTO inventory_movement (uid, created_at, updated_at, version, component_id, location_id, qty, direction, reason, ref_type, at, note) "
            "VALUES (%s, now(), now(), 1, %s, %s, 1, 'IN', 'PURCHASE', '', now(), '') RETURNING id"
        )
        assert _as_role(role, insert, [str(uuid.uuid4()), movement.component_id, movement.location_id])[0][0]
        assert _as_role(role, "SELECT count(*) FROM inventory_movement") == [(2,)]
        assert _as_role(role, "SELECT qty FROM inventory_balance")[0][0] == 11
        for statement in ("UPDATE inventory_movement SET note = 'tampered'", "DELETE FROM inventory_movement", "TRUNCATE inventory_movement"):
            with pytest.raises(ProgrammingError, match="permission denied"):
                _as_role(role, statement)
        assert not Movement.objects.filter(note="tampered").exists() and Movement.objects.count() == 2

    def test_owner_retains_full_control(self):
        apply_append_only_privileges(_temporary_role())
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


class TestMigrationStepAndCommand:
    migration = importlib.import_module("inventory.migrations.0001_initial")

    def _migrate(self):
        self.migration.apply_append_only(None, SimpleNamespace(connection=connection))

    def test_the_migration_applies_the_revoke_for_a_separate_app_role(self):
        role = _temporary_role()
        with override_settings(DB_APP_ROLE=role):
            self._migrate()
        assert privileges_of(role)["UPDATE"] is False and privileges_of(role)["INSERT"] is True

    @pytest.mark.parametrize("configured", ["", "   "])
    def test_no_app_role_is_a_no_op(self, configured):
        with override_settings(DB_APP_ROLE=configured):
            self._migrate()

    def test_same_role_as_the_owner_is_a_no_op(self):
        with override_settings(DB_APP_ROLE=table_owner()):
            self._migrate()
        assert privileges_of(table_owner())["UPDATE"] is True

    def test_the_command_is_idempotent(self):
        role = _temporary_role()
        for _ in range(2):
            out = io.StringIO()
            call_command("ensure_inventory_append_only", "--app-role", role, stdout=out)
            assert "append-only" in out.getvalue()
        assert privileges_of(role)["DELETE"] is False

    def test_the_command_in_single_role_setups_and_with_a_bad_role(self):
        out = io.StringIO()
        with override_settings(DB_APP_ROLE=""):
            call_command("ensure_inventory_append_only", stdout=out)
        call_command("ensure_inventory_append_only", "--app-role", table_owner(), stdout=out)
        assert "left unchanged" in out.getvalue()
        with pytest.raises(CommandError, match="does not exist"):
            call_command("ensure_inventory_append_only", "--app-role", "flarize_no_such_role", stdout=out)

    def test_every_deploy_runs_it_after_migrate(self):
        script = (Path(settings.BASE_DIR) / "deploy" / "release.sh").read_text()
        assert script.index("manage.py migrate --noinput") < script.index("manage.py ensure_inventory_append_only")
