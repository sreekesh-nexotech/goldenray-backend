"""Maintenance of the partitioned ``audit_log`` table: monthly partitions and append-only privileges.

Partitions are monthly ranges on ``at`` with **UTC** month boundaries, named ``audit_log_yYYYYmMM``. Rows whose month
has no partition land in ``audit_log_default``; when that month's partition is created later, its rows are moved out
of the default partition in the same transaction (otherwise Postgres refuses to create the partition).

Privileges (standard §3.2 "append-only ledgers via REVOKE"): the application connects as ``DB_APP_ROLE`` while
migrations and this maintenance run as the table owner. The app role gets ``SELECT, INSERT`` on the parent (and the
id sequence) and loses ``UPDATE, DELETE, TRUNCATE``; it gets nothing on the partitions themselves (queries through
the parent never check partition privileges), so default privileges granted by ops can never re-open a partition
for direct writes. Both functions must run as the owner — see ``docs/ops/audit-log.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from datetime import timezone as dt_timezone

from django.db import connection, transaction
from django.utils import timezone

PARENT_TABLE = "audit_log"
DEFAULT_PARTITION = "audit_log_default"
ID_SEQUENCE = "audit_log_id_seq"


class PrivilegeError(RuntimeError):
    """The requested app role cannot be made append-only (missing, is the owner, or inherits the owner's rights)."""


@dataclass(frozen=True)
class MonthRange:
    start: date  # first day of the month

    @property
    def end(self) -> date:
        return add_months(self.start, 1)

    @property
    def name(self) -> str:
        return f"{PARENT_TABLE}_y{self.start.year:04d}m{self.start.month:02d}"

    @property
    def lower(self) -> str:
        return _timestamp_literal(self.start)

    @property
    def upper(self) -> str:
        return _timestamp_literal(self.end)


@dataclass(frozen=True)
class PartitionResult:
    name: str
    created: bool
    moved_rows: int = 0


def month_start(value: date | datetime) -> date:
    if isinstance(value, datetime):
        value = value.astimezone(dt_timezone.utc).date() if timezone.is_aware(value) else value.date()
    return date(value.year, value.month, 1)


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def month_ranges(first: date, months: int) -> list[MonthRange]:
    start = month_start(first)
    return [MonthRange(add_months(start, offset)) for offset in range(months)]


def _timestamp_literal(day: date) -> str:
    # Built from integers only (never from input), so it is safe to inline into DDL.
    return f"'{day.year:04d}-{day.month:02d}-{day.day:02d} 00:00:00+00'"


def _q(name: str) -> str:
    return connection.ops.quote_name(name)


def existing_partitions() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class child ON child.oid = pg_inherits.inhrelid
            JOIN pg_namespace ns ON ns.oid = parent.relnamespace
            WHERE parent.relname = %s AND ns.nspname = current_schema()
            """,
            [PARENT_TABLE],
        )
        return {row[0] for row in cursor.fetchall()}


def _create_month(cursor, month: MonthRange) -> int:
    """Create one monthly partition; returns how many rows were moved out of the default partition."""
    cursor.execute(f"LOCK TABLE {_q(DEFAULT_PARTITION)} IN ACCESS EXCLUSIVE MODE")
    cursor.execute(f"SELECT count(*) FROM {_q(DEFAULT_PARTITION)} WHERE at >= {month.lower} AND at < {month.upper}")
    stranded = cursor.fetchone()[0]
    if not stranded:
        cursor.execute(f"CREATE TABLE {_q(month.name)} PARTITION OF {_q(PARENT_TABLE)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
        return 0
    cursor.execute(f"CREATE TABLE {_q(month.name)} (LIKE {_q(PARENT_TABLE)} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    cursor.execute(f"INSERT INTO {_q(month.name)} SELECT * FROM {_q(DEFAULT_PARTITION)} WHERE at >= {month.lower} AND at < {month.upper}")
    cursor.execute(f"DELETE FROM {_q(DEFAULT_PARTITION)} WHERE at >= {month.lower} AND at < {month.upper}")
    cursor.execute(f"ALTER TABLE {_q(PARENT_TABLE)} ATTACH PARTITION {_q(month.name)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
    return stranded


def ensure_partitions(months: int = 3, *, today: date | None = None, app_role: str | None = None) -> list[PartitionResult]:
    """Make sure partitions exist for the current month and the next ``months - 1`` months (idempotent).

    With ``app_role``, the new partitions are closed to that role (see module docstring).
    """
    if months < 1:
        raise ValueError("months must be at least 1.")
    existing = existing_partitions()
    results: list[PartitionResult] = []
    for month in month_ranges(today or timezone.now().date(), months):
        if month.name in existing:
            results.append(PartitionResult(month.name, created=False))
            continue
        with transaction.atomic(), connection.cursor() as cursor:
            moved = _create_month(cursor, month)
            if app_role:
                _close_partition(cursor, month.name, app_role)
        results.append(PartitionResult(month.name, created=True, moved_rows=moved))
    return results


def _role_exists(cursor, role: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
    return cursor.fetchone() is not None


def _close_partition(cursor, partition: str, role: str) -> None:
    cursor.execute(f"REVOKE ALL ON TABLE {_q(partition)} FROM {_q(role)}")


def table_owner() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass", [PARENT_TABLE])
        return cursor.fetchone()[0]


def check_app_role(role: str) -> None:
    """Raise ``PrivilegeError`` unless ``role`` exists and is neither the owner nor a member of the owner role."""
    with connection.cursor() as cursor:
        if not _role_exists(cursor, role):
            raise PrivilegeError(f"Database role {role!r} (DB_APP_ROLE) does not exist.")
        owner = table_owner()
        if role == owner:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} owns {PARENT_TABLE}; the application must connect as a separate role.")
        cursor.execute("SELECT pg_has_role(%s, %s, 'MEMBER'), rolsuper FROM pg_roles WHERE rolname = %s", [role, owner, role])
        is_member, is_superuser = cursor.fetchone()
        if is_member or is_superuser:
            raise PrivilegeError(f"DB_APP_ROLE {role!r} inherits the owner's privileges (member of {owner!r} or superuser); REVOKE would have no effect.")


def apply_append_only_privileges(role: str) -> list[str]:
    """Grant ``role`` exactly what appending needs and revoke everything that changes or removes rows.

    Returns the partitions that were closed. Raises ``PrivilegeError`` for an unusable role (fail closed).
    """
    check_app_role(role)
    partitions = sorted(existing_partitions())
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"GRANT SELECT, INSERT ON TABLE {_q(PARENT_TABLE)} TO {_q(role)}")
        cursor.execute(f"GRANT USAGE, SELECT ON SEQUENCE {_q(ID_SEQUENCE)} TO {_q(role)}")
        cursor.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {_q(PARENT_TABLE)} FROM {_q(role)}")
        for partition in partitions:
            _close_partition(cursor, partition, role)
    return partitions


def privileges_of(role: str) -> dict[str, bool]:
    """Effective privileges of ``role`` on the parent table (used by tests and the ops checklist)."""
    with connection.cursor() as cursor:
        result = {}
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            cursor.execute("SELECT has_table_privilege(%s, %s, %s)", [role, PARENT_TABLE, privilege])
            result[privilege] = cursor.fetchone()[0]
        return result


def connected_as_owner() -> bool:
    """True when the current connection may create partitions (the owner, a member of it, or a superuser)."""
    owner = table_owner()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", [owner])
        return bool(cursor.fetchone()[0])


def missing_upcoming(months: int = 2, *, today: date | None = None) -> list[str]:
    """Names of the partitions for the current and next ``months - 1`` months that do not exist yet."""
    existing = existing_partitions()
    return [month.name for month in month_ranges(today or timezone.now().date(), months) if month.name not in existing]
