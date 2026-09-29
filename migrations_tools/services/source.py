"""Legacy sources: a read-only PostgreSQL connection (``--source-url``) or in-memory tables (tests, fixtures).

Rows come back as plain dicts keyed by the legacy column names — what every ``<app>.services.legacy_import`` function
takes. The PostgreSQL source opens its session with ``default_transaction_read_only`` and ``read_only``, so no
statement can write to the legacy database even by mistake; tables are read ordered by ``id`` (insertion order, what
the legacy APIs listed) or by every column when a table has no ``id``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit


class SourceError(Exception):
    """The legacy source cannot be opened or read."""


def checksum(rows: list[dict]) -> str:
    """SHA-256 of the rows as canonical JSON (keys sorted; dates with their microseconds, decimals and UUIDs as text)."""
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def redact_url(url: str) -> str:
    """The URL without its password (for reports and audit rows)."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
    return parts._replace(netloc=netloc).geturl()


class Source:
    """Interface: ``has_table``, ``rows``, ``count``, ``close``."""

    label = ""

    def table_names(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def has_table(self, table: str) -> bool:
        return table in self.table_names()

    def rows(self, table: str) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def count(self, table: str) -> int:
        return len(self.rows(table)) if self.has_table(table) else 0

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TablesSource(Source):
    """In-memory ``{table: [row, …]}`` (committed JSON fixtures)."""

    def __init__(self, tables: Mapping[str, list[dict]], label: str = "fixture"):
        self.tables = {name: list(rows) for name, rows in tables.items()}
        self.label = label

    @classmethod
    def from_file(cls, path: str | Path) -> TablesSource:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("tables", data), label=str(path))

    def table_names(self) -> list[str]:
        return sorted(self.tables)

    def rows(self, table: str) -> list[dict]:
        return [dict(row) for row in self.tables.get(table, [])]


class PostgresSource(Source):
    """A read-only session on the legacy PostgreSQL database."""

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        self.label = redact_url(url)
        try:
            self.conn = psycopg.connect(url, row_factory=dict_row, options="-c default_transaction_read_only=on", autocommit=False)
        except psycopg.Error as exc:
            raise SourceError(f"cannot connect to {self.label}: {exc.__class__.__name__}") from exc
        self.conn.read_only = True
        self._tables = {row["table_name"] for row in self._fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")}

    def _fetch(self, sql: str, params=()) -> list[dict]:
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def _columns(self, table: str) -> list[str]:
        rows = self._fetch("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position", (table,))
        return [row["column_name"] for row in rows]

    def table_names(self) -> list[str]:
        return sorted(self._tables)

    def has_table(self, table: str) -> bool:
        return table in self._tables

    def rows(self, table: str) -> list[dict]:
        from psycopg import sql

        if not self.has_table(table):
            return []
        columns = self._columns(table)
        order = [sql.Identifier("id")] if "id" in columns else [sql.Identifier(column) for column in columns]
        query = sql.SQL("SELECT * FROM {} ORDER BY {}").format(sql.Identifier(table), sql.SQL(", ").join(order))
        with self.conn.cursor() as cursor:
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def count(self, table: str) -> int:
        from psycopg import sql

        if not self.has_table(table):
            return 0
        with self.conn.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
            return cursor.fetchone()["n"]

    def close(self) -> None:
        self.conn.rollback()
        self.conn.close()


def open_source(url: str | None = None, *, fixture: str | None = None) -> Source:
    if fixture:
        return TablesSource.from_file(fixture)
    if not url or not url.startswith(("postgresql://", "postgres://")):
        raise SourceError("--source-url must be a postgresql:// URL of the legacy database.")
    return PostgresSource(url)
