"""Build the eSSL fixture databases of migration-ops (run by hand, never by pytest). No production eSSL copy exists here.

Both databases are created by the eSSL application itself: its alembic chain (``alembic upgrade head``), then

* ``--seed`` — its own ``scripts/seed.py`` (roles, admin login, offices HO/BR1/BR2, shifts GEN/NIGHT, device MARS-01):
  the configuration every eSSL install starts from. ``essl_seeded.json`` is its read-only ``SELECT *`` export (the admin
  bcrypt hash replaced by a hash of a random string: the committed fixture holds no usable credential);
* ``--load FILE`` — the rows of a ``SELECT *`` export captured from a running eSSL (default: the attendance package's
  ``attendance/tests/legacy/essl_attendance_tables.json``: eSSL's own endpoints, agent protocol and ADMS receiver drove a
  private eSSL through a month of punches; names masked, e-mails/phones dropped) inserted into the migrated schema, so
  the importer reads a real eSSL database with punches, v3 stored days, leave, holidays and a collapsed duplicate.
  Password and agent-token hashes the export dropped are set to bcrypt hashes of random strings (NOT NULL columns).

Usage (private databases only; the eSSL venv is /home/user/.venvs/essl)::

    createdb essl_wp_migration_ops
    cd <essl>/backend && DATABASE_URL=postgresql://…/essl_wp_migration_ops ENVIRONMENT=local alembic upgrade head
    DATABASE_URL=… ENVIRONMENT=local python -m scripts.seed
    python migrations_tools/tests/fixtures/ops/essl/build_essl_fixture.py --dsn postgresql://…/essl_wp_migration_ops --export

    createdb essl_wp_migration_ops_full && (alembic upgrade head as above, no seed)
    python migrations_tools/tests/fixtures/ops/essl/build_essl_fixture.py --dsn postgresql://…/essl_wp_migration_ops_full --load
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import secrets
import uuid
from pathlib import Path

import bcrypt
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3].parent  # the repository root
DEFAULT_LOAD = ROOT / "attendance/tests/legacy/essl_attendance_tables.json"
# FK order of the eSSL schema
ORDER = (
    "roles",
    "users",
    "shifts",
    "offices",
    "employees",
    "holidays",
    "leave_records",
    "attendance_rules",
    "agents",
    "devices",
    "device_users",
    "sync_logs",
    "protocol_mappings",
    "adms_unknown_devices",
    "adms_requests",
    "attendance_raw",
    "attendance",
)
HASHES = {"users": "password_hash", "agents": "token_hash"}


def random_hash() -> str:
    return bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(rounds=4)).decode()


def plain(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def export(conn) -> dict:
    tables = {}
    for table in ORDER:
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table)))
            rows = [{key: plain(value) for key, value in row.items()} for row in cursor.fetchall()]
        for row in rows:
            if HASHES.get(table) in row and row[HASHES[table]]:
                row[HASHES[table]] = random_hash()
        tables[table] = rows
    return {"source": "eSSL alembic head + scripts/seed.py (private database)", "tables": tables}


def load(conn, data: dict) -> dict:
    counts = {}
    with conn.cursor() as cursor:
        for table in ORDER:
            rows = data.get(table) or []
            cursor.execute("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s", (table,))
            columns = {row["column_name"]: row["is_nullable"] for row in cursor.fetchall()}
            for row in rows:
                values = {key: (Jsonb(value) if isinstance(value, (dict, list)) else value) for key, value in row.items() if key in columns}
                hashed = HASHES.get(table)
                if hashed in columns and not values.get(hashed):
                    values[hashed] = random_hash()
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, values)), sql.SQL(", ").join(sql.Placeholder() * len(values)))
                cursor.execute(query, list(values.values()))
            if rows and "id" in columns:
                cursor.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({}, 'id'), (SELECT max(id) FROM {}))").format(sql.Literal(table), sql.Identifier(table)))
            counts[table] = len(rows)
    conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--export", action="store_true", help="write essl_seeded.json from the seeded database")
    parser.add_argument("--load", nargs="?", const=str(DEFAULT_LOAD), help="insert a SELECT * export into the migrated (unseeded) database")
    args = parser.parse_args()
    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        if args.load:
            data = json.loads(Path(args.load).read_text(encoding="utf-8"))
            print(json.dumps(load(conn, data.get("tables", data))))
        if args.export:
            (HERE / "essl_seeded.json").write_text(json.dumps(export(conn), indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
