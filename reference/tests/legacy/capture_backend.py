#!/usr/bin/env python3
"""Record the legacy reference tables and their public payloads for the parity tests (read-only; not run by pytest).

    python reference/tests/legacy/capture_backend.py --db legacy_goldenapp --url http://127.0.0.1:18012 \\
        --flarize /home/user/flarize-main/flarize/data/quotation-content.json

Writes ``reference/tests/legacy/backend_reference.json``: the source rows (``SELECT *``) of ``kseb_tariffs``,
``device_types``, ``wattages``, ``room_size``, ``ev_cars``, ``ev_scooters`` and the post-office rows of a sample of
pincodes (every multi-district / multi-division pincode plus single-office ones), the legacy responses of
``GET /api/<list>/`` (the pincode response is cut to the sampled rows — the legacy endpoint lists all 5,057), and
Flarize's appliance master. Credentials come from the libpq environment (``PGPASSWORD`` …). The database is only read (``SET default_transaction_read_only``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import urllib.request
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
LISTS = {"tariffs": "kseb_tariffs", "device-types": "device_types", "wattages": "wattages", "room-sizes": "room_size", "ev-cars": "ev_cars", "ev-scooters": "ev_scooters"}
SAMPLE_SQL = """
    WITH spread AS (
        SELECT pincode FROM pincodes GROUP BY pincode HAVING count(DISTINCT district) > 1 OR count(DISTINCT division) > 1
    ), singles AS (
        SELECT pincode FROM pincodes GROUP BY pincode HAVING count(*) = 1 ORDER BY pincode LIMIT 5
    ), busy AS (
        SELECT pincode FROM pincodes GROUP BY pincode ORDER BY count(*) DESC, pincode LIMIT 3
    )
    SELECT * FROM pincodes WHERE pincode IN (SELECT pincode FROM spread UNION SELECT pincode FROM singles UNION SELECT pincode FROM busy)
    ORDER BY id
"""


def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def fetch(url: str):
    with urllib.request.urlopen(url) as response:  # noqa: S310 - local legacy server
        return json.loads(response.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--flarize", required=True, type=Path)
    args = parser.parse_args()
    base = args.url.rstrip("/")

    rows = {}
    with psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user=os.environ.get("PGUSER", "postgres"), dbname=args.db, row_factory=dict_row) as connection:
        connection.execute("SET default_transaction_read_only = on")
        for table in LISTS.values():
            rows[table] = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()  # noqa: S608 - fixed table names
        rows["pincodes"] = connection.execute(SAMPLE_SQL).fetchall()

    responses = {key: fetch(f"{base}/api/{key}/") for key in LISTS}
    sampled = {row["id"] for row in rows["pincodes"]}
    responses["pincodes"] = [row for row in fetch(f"{base}/api/pincodes/") if row["id"] in sampled]

    content = json.loads(args.flarize.read_text())
    appliances = content["published"]["content"]["appliances"]["master"]

    payload = {"source": {"db": args.db, "url": base, "flarize": str(args.flarize)}, "rows": rows, "responses": responses, "appliances": appliances}
    target = HERE / "backend_reference.json"
    target.write_text(json.dumps(payload, indent=1, sort_keys=True, default=_json_default, ensure_ascii=False) + "\n")
    print(f"wrote {target}: " + ", ".join(f"{table} {len(values)}" for table, values in rows.items()) + f", appliances {len(appliances)}")


if __name__ == "__main__":
    main()
