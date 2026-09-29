"""Upkeep of the partitioned, append-only ``attendance_raw_punch`` table.

**Partitions** are monthly ranges on ``device_time`` (the terminal's wall clock, a ``timestamp``), named
``attendance_raw_punch_yYYYYmMM``. A punch of a month without a partition lands in ``attendance_raw_punch_default``;
when the month's partition is created later, its rows are moved out of the default partition in the same transaction
(Postgres refuses to create the partition otherwise). Punches are never purged (the raw table is the evidence of
every processed day).

**Privileges** (standard §3.2 "append-only ledgers via REVOKE", like ``audit_log``): the application connects as
``DB_APP_ROLE`` while migrations and this upkeep run as the table owner. The app role keeps ``SELECT, INSERT`` on the
parent (and the id sequence) and loses ``UPDATE, DELETE, TRUNCATE``; it gets nothing on the partitions themselves
(queries through the parent never check partition privileges), so a broad ``GRANT … ON ALL TABLES`` by ops can never
open a partition for direct writes.

Both need the owner: ``manage.py maintain_attendance_punches`` runs on every deploy (``deploy/release.sh``); the
monthly Beat task creates partitions only when its connection may.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.db import connection, transaction
from django.utils import timezone

PARENT_TABLE = "attendance_raw_punch"
DEFAULT_PARTITION = "attendance_raw_punch_default"
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")


class PrivilegeError(RuntimeError):
    """The app role cannot be made append-only (missing, the owner, or inheriting the owner's rights)."""


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class Month:
    start: date

    @property
    def end(self) -> date:
        return add_months(self.start, 1)

    @property
    def name(self) -> str:
        return f"{PARENT_TABLE}_y{self.start.year:04d}m{self.start.month:02d}"

    @property
    def lower(self) -> str:
        return _literal(self.start)

    @property
    def upper(self) -> str:
        return _literal(self.end)


def _literal(day: date) -> str:
    return f"'{day.year:04d}-{day.month:02d}-{day.day:02d} 00:00:00'"  # built from integers only, safe in DDL


def _q(name: str) -> str:
    return connection.ops.quote_name(name)


def existing_partitions() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT child.relname FROM pg_inherits
            JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class child ON child.oid = pg_inherits.inhrelid
            JOIN pg_namespace ns ON ns.oid = parent.relnamespace
            WHERE parent.relname = %s AND ns.nspname = current_schema()
            """,
            [PARENT_TABLE],
        )
        return {row[0] for row in cursor.fetchall()}


def table_owner() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass", [PARENT_TABLE])
        return cursor.fetchone()[0]


def connected_as_owner() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_has_role(current_user, pg_get_userbyid(relowner), 'MEMBER') FROM pg_class WHERE oid = %s::regclass", [PARENT_TABLE])
        return bool(cursor.fetchone()[0])


def _create(cursor, month: Month) -> int:
    """Create one monthly partition; returns how many rows were moved out of the default partition."""
    cursor.execute(f"LOCK TABLE {_q(DEFAULT_PARTITION)} IN ACCESS EXCLUSIVE MODE")
    cursor.execute(f"SELECT count(*) FROM {_q(DEFAULT_PARTITION)} WHERE device_time >= {month.lower} AND device_time < {month.upper}")
    stranded = cursor.fetchone()[0]
    if not stranded:
        cursor.execute(f"CREATE TABLE {_q(month.name)} PARTITION OF {_q(PARENT_TABLE)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
        return 0
    cursor.execute(f"CREATE TABLE {_q(month.name)} (LIKE {_q(PARENT_TABLE)} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    cursor.execute(f"INSERT INTO {_q(month.name)} SELECT * FROM {_q(DEFAULT_PARTITION)} WHERE device_time >= {month.lower} AND device_time < {month.upper}")
    cursor.execute(f"DELETE FROM {_q(DEFAULT_PARTITION)} WHERE device_time >= {month.lower} AND device_time < {month.upper}")
    cursor.execute(f"ALTER TABLE {_q(PARENT_TABLE)} ATTACH PARTITION {_q(month.name)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
    return stranded


def ensure_months(months: list[date], *, app_role: str | None = None) -> list[dict]:
    """Create the partitions of the given months (any day of the month) that do not exist yet (idempotent)."""
    existing = existing_partitions()
    results = []
    for month in sorted({Month(value.replace(day=1)) for value in months}, key=lambda item: item.start):
        if month.name in existing:
            continue
        with transaction.atomic(), connection.cursor() as cursor:
            moved = _create(cursor, month)
            if app_role:
                cursor.execute(f"REVOKE ALL ON TABLE {_q(month.name)} FROM {_q(app_role)}")
        results.append({"name": month.name, "moved_rows": moved})
    return results


def ensure_partitions(months: int = 3, *, today: date | None = None, app_role: str | None = None) -> list[dict]:
    """The current month and the next ``months - 1`` (idempotent); returns the partitions created."""
    if months < 1:
        raise ValueError("months must be at least 1.")
    start = (today or timezone.now().date()).replace(day=1)
    return ensure_months([add_months(start, offset) for offset in range(months)], app_role=app_role)


def missing_upcoming(months: int = 2, *, today: date | None = None) -> list[str]:
    start = (today or timezone.now().date()).replace(day=1)
    existing = existing_partitions()
    names = [Month(add_months(start, offset)).name for offset in range(months)]
    return [name for name in names if name not in existing]


def default_partition_rows() -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {_q(DEFAULT_PARTITION)}")
        return cursor.fetchone()[0]


def check_app_role(role: str) -> None:
    """Raise :class:`PrivilegeError` unless ``role`` exists and is neither the owner nor a member of it (fail closed)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if cursor.fetchone() is None:
            raise PrivilegeError(f"Database role {role!r} (DB_APP_ROLE) does not exist.")
        owner = table_owner()
        if role == owner:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} owns {PARENT_TABLE}; the application must connect as a separate role.")
        cursor.execute("SELECT pg_has_role(%s, %s, 'MEMBER'), rolsuper FROM pg_roles WHERE rolname = %s", [role, owner, role])
        is_member, is_superuser = cursor.fetchone()
        if is_member or is_superuser:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} inherits the owner's privileges (member of {owner!r} or superuser); REVOKE would have no effect.")


def apply_append_only_privileges(role: str) -> list[str]:
    """Grant ``role`` exactly what appending and reading need; revoke everything that changes or removes punches.

    Returns the partitions that were closed to the role.
    """
    check_app_role(role)
    partitions = sorted(existing_partitions())
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [PARENT_TABLE])
        sequence = cursor.fetchone()[0]
        cursor.execute(f"GRANT SELECT, INSERT ON TABLE {_q(PARENT_TABLE)} TO {_q(role)}")
        if sequence:  # already quoted/qualified by pg_get_serial_sequence
            cursor.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {_q(role)}")
        cursor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {_q(PARENT_TABLE)} FROM {_q(role)}")
        for partition in partitions:
            cursor.execute(f"REVOKE ALL ON TABLE {_q(partition)} FROM {_q(role)}")
    return partitions


def privileges_of(role: str, table: str = PARENT_TABLE) -> dict[str, bool]:
    """Effective privileges of ``role`` on ``table`` (tests and the ops checklist)."""
    with connection.cursor() as cursor:
        result = {}
        for privilege in PRIVILEGES:
            cursor.execute("SELECT has_table_privilege(%s, %s, %s)", [role, table, privilege])
            result[privilege] = cursor.fetchone()[0]
        return result


def maintain(months: int = 3, *, app_role: str | None = None) -> dict:
    """Beat / command entry point: upcoming partitions when this connection owns the table."""
    owner = connected_as_owner()
    created = ensure_partitions(months, app_role=app_role) if owner else []
    return {"owner": owner, "created": [item["name"] for item in created], "default_partition_rows": default_partition_rows()}
