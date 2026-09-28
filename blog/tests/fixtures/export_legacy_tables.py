#!/usr/bin/env python3
"""Export legacy tables, read-only, to a committed JSON fixture (``{table: [row, …]}``).

The legacy importers (``blog.services.legacy_import``, ``seo.services.legacy_import``) take plain row dicts; these
fixtures are exactly what they receive in production from the read-only source connection, so the parity tests load
the same data the legacy servers serve. Personal data is masked (e-mail addresses, phone numbers, user names,
passwords); author bylines stay because they are published on the website and part of the delivery payload.

    python blog/tests/fixtures/export_legacy_tables.py --database legacy_blog_cms --out blog/tests/fixtures/legacy_cms/tables.json \
        --table catalog_collection --table content_entry …

The transaction is ``READ ONLY``: the script can never write to a legacy database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import re
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

PERSONAL_COLUMN_RE = re.compile(r"(e_?mail|phone|username|password|first_name|last_name)", re.IGNORECASE)


def _jsonable(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def _mask(column: str, value):
    if value in (None, "") or not PERSONAL_COLUMN_RE.search(column):
        return value
    return f"masked-{column}"


def export(database: str, tables: list[str]) -> dict[str, list[dict]]:
    conninfo = f"host={os.environ.get('PGHOST', 'localhost')} port={os.environ.get('PGPORT', '5432')} user={os.environ.get('PGUSER', 'postgres')} dbname={database}"
    password = os.environ.get("PGPASSWORD", "postgres")
    result: dict[str, list[dict]] = {}
    with psycopg.connect(conninfo, password=password, row_factory=dict_row) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table)))
                result[table] = [{column: _mask(column, _jsonable(value)) for column, value in row.items()} for row in cursor.fetchall()]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True)
    parser.add_argument("--table", action="append", required=True, dest="tables")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    data = export(args.database, args.tables)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print({table: len(rows) for table, rows in data.items()})


if __name__ == "__main__":
    main()
