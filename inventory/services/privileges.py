"""Append-only privileges of ``inventory_movement`` (standard §3.2 "append-only ledgers via REVOKE", like ``audit_log``).

The application connects as ``DB_APP_ROLE`` while migrations run as the table owner (docs/ops/audit-log.md). The app
role keeps ``SELECT, INSERT`` on ``inventory_movement`` (and the id sequence) and loses ``UPDATE, DELETE, TRUNCATE``;
it keeps ``SELECT`` on the ``inventory_balance`` view. Foreign-key actions (``SET NULL`` of ``by``/``created_by`` when a
user row is removed) run with the owner's rights, so they are unaffected.

Applied by ``inventory/migrations/0001_initial.py`` when ``DB_APP_ROLE`` is set and differs from the migrating role,
and re-applied (idempotently) by ``manage.py ensure_inventory_append_only`` on every deploy, so a role configured
after the first migration, or a broad ``GRANT … ON ALL TABLES`` by ops, is narrowed again. Everything here must run
as the owner.
"""

from __future__ import annotations

from django.db import connection, transaction

MOVEMENT_TABLE = "inventory_movement"
BALANCE_VIEW = "inventory_balance"
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")


class PrivilegeError(RuntimeError):
    """The app role cannot be made append-only (missing, the owner, or inheriting the owner's rights)."""


def _q(name: str) -> str:
    return connection.ops.quote_name(name)


def table_owner() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass", [MOVEMENT_TABLE])
        return cursor.fetchone()[0]


def check_app_role(role: str) -> None:
    """Raise :class:`PrivilegeError` unless ``role`` exists and is neither the owner nor a member of it (fail closed)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if cursor.fetchone() is None:
            raise PrivilegeError(f"Database role {role!r} (DB_APP_ROLE) does not exist.")
        owner = table_owner()
        if role == owner:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} owns {MOVEMENT_TABLE}; the application must connect as a separate role.")
        cursor.execute("SELECT pg_has_role(%s, %s, 'MEMBER'), rolsuper FROM pg_roles WHERE rolname = %s", [role, owner, role])
        is_member, is_superuser = cursor.fetchone()
        if is_member or is_superuser:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} inherits the owner's privileges (member of {owner!r} or superuser); REVOKE would have no effect.")


def id_sequence() -> str | None:
    """The sequence behind ``inventory_movement.id`` (identity column), schema-qualified, or ``None``."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [MOVEMENT_TABLE])
        return cursor.fetchone()[0]


def apply_append_only_privileges(role: str) -> None:
    """Grant ``role`` exactly what appending and reading need; revoke everything that changes or removes movements."""
    check_app_role(role)
    sequence = id_sequence()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"GRANT SELECT, INSERT ON TABLE {_q(MOVEMENT_TABLE)} TO {_q(role)}")
        if sequence:  # already quoted/qualified by pg_get_serial_sequence
            cursor.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {_q(role)}")
        cursor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {_q(MOVEMENT_TABLE)} FROM {_q(role)}")
        cursor.execute(f"GRANT SELECT ON TABLE {_q(BALANCE_VIEW)} TO {_q(role)}")


def privileges_of(role: str, table: str = MOVEMENT_TABLE) -> dict[str, bool]:
    """Effective privileges of ``role`` on ``table`` (tests and the ops checklist)."""
    with connection.cursor() as cursor:
        result = {}
        for privilege in PRIVILEGES:
            cursor.execute("SELECT has_table_privilege(%s, %s, %s)", [role, table, privilege])
            result[privilege] = cursor.fetchone()[0]
        return result
