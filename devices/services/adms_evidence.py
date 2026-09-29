"""Maintenance of the partitioned ``devices_adms_request`` table: monthly partitions and the 30-day retention.

Partitions are monthly ranges on ``received_at`` with UTC month boundaries, named ``devices_adms_request_yYYYYmMM``;
rows of a month without a partition land in ``devices_adms_request_default`` (moved out when the month's partition is
created later, in the same transaction). :func:`purge` keeps ``DEVICES_ADMS_RETENTION_DAYS`` (30) days of evidence:
partitions entirely older than the cutoff are dropped (cheap), the remaining older rows are deleted.

DDL needs the table owner (``manage.py maintain_adms_evidence``, run by ``deploy/release.sh`` as the owner role, like
``ensure_audit_partitions``). The daily Beat task does both when its connection may; otherwise it only deletes rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.db import connection, transaction
from django.utils import timezone

from devices.services.common import setting

PARENT_TABLE = "devices_adms_request"
DEFAULT_PARTITION = "devices_adms_request_default"


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


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _literal(day: date) -> str:
    return f"'{day.year:04d}-{day.month:02d}-{day.day:02d} 00:00:00+00'"  # built from integers only, safe in DDL


def _q(name: str) -> str:
    return connection.ops.quote_name(name)


def retention_days() -> int:
    return int(setting("DEVICES_ADMS_RETENTION_DAYS", 30))


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


def month_of(name: str) -> Month | None:
    prefix = f"{PARENT_TABLE}_y"
    if not name.startswith(prefix):
        return None
    try:
        year, month = int(name[len(prefix) : len(prefix) + 4]), int(name[-2:])
        return Month(date(year, month, 1))
    except ValueError:
        return None


def connected_as_owner() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_has_role(current_user, pg_get_userbyid(relowner), 'MEMBER') FROM pg_class WHERE oid = %s::regclass", [PARENT_TABLE])
        return bool(cursor.fetchone()[0])


def _create(cursor, month: Month) -> int:
    cursor.execute(f"LOCK TABLE {_q(DEFAULT_PARTITION)} IN ACCESS EXCLUSIVE MODE")
    cursor.execute(f"SELECT count(*) FROM {_q(DEFAULT_PARTITION)} WHERE received_at >= {month.lower} AND received_at < {month.upper}")
    stranded = cursor.fetchone()[0]
    if not stranded:
        cursor.execute(f"CREATE TABLE {_q(month.name)} PARTITION OF {_q(PARENT_TABLE)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
        return 0
    cursor.execute(f"CREATE TABLE {_q(month.name)} (LIKE {_q(PARENT_TABLE)} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    cursor.execute(f"INSERT INTO {_q(month.name)} SELECT * FROM {_q(DEFAULT_PARTITION)} WHERE received_at >= {month.lower} AND received_at < {month.upper}")
    cursor.execute(f"DELETE FROM {_q(DEFAULT_PARTITION)} WHERE received_at >= {month.lower} AND received_at < {month.upper}")
    cursor.execute(f"ALTER TABLE {_q(PARENT_TABLE)} ATTACH PARTITION {_q(month.name)} FOR VALUES FROM ({month.lower}) TO ({month.upper})")
    return stranded


def ensure_partitions(months: int = 3, *, today: date | None = None) -> list[str]:
    """Create the partitions of the current month and the next ``months - 1`` (idempotent); returns those created."""
    if months < 1:
        raise ValueError("months must be at least 1.")
    start = (today or timezone.now().date()).replace(day=1)
    existing = existing_partitions()
    created = []
    for offset in range(months):
        month = Month(add_months(start, offset))
        if month.name in existing:
            continue
        with transaction.atomic(), connection.cursor() as cursor:
            _create(cursor, month)
        created.append(month.name)
    return created


def missing_upcoming(months: int = 2, *, today: date | None = None) -> list[str]:
    start = (today or timezone.now().date()).replace(day=1)
    existing = existing_partitions()
    return [Month(add_months(start, offset)).name for offset in range(months) if Month(add_months(start, offset)).name not in existing]


def purge(*, days: int | None = None, drop_partitions: bool = True, now=None) -> dict:
    """Delete evidence older than the retention window. Returns ``{"cutoff", "dropped", "deleted"}``."""
    cutoff = (now or timezone.now()) - timedelta(days=retention_days() if days is None else days)
    dropped = []
    if drop_partitions:
        for name in sorted(existing_partitions()):
            month = month_of(name)
            if month is not None and datetime.combine(month.end, time.min, tzinfo=dt_timezone.utc) <= cutoff:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"DROP TABLE {_q(name)}")
                dropped.append(name)
    from devices.models import AdmsRequest

    with transaction.atomic():
        deleted, _ = AdmsRequest.objects.filter(received_at__lt=cutoff).delete()
    return {"cutoff": cutoff, "dropped": dropped, "deleted": deleted}


def maintain(months: int = 3) -> dict:
    """Beat / command entry point: partitions (owner only), then the retention purge."""
    owner = connected_as_owner()
    created = ensure_partitions(months) if owner else []
    result = purge(drop_partitions=owner)
    return {"owner": owner, "created": created, **result}
