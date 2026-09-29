#!/usr/bin/env python3
"""Record the legacy CMS careers data and delivery payloads the parity tests replay (read-only; not run by pytest).

    python careers/tests/legacy/capture_cms.py --db legacy_blog_cms --url http://127.0.0.1:18009 --scenario shared
    python careers/tests/legacy/capture_cms.py --db legacy_blog_cms_careers_reference --url http://127.0.0.1:18161 --scenario enriched

Writes ``careers/tests/legacy/cms_<scenario>.json``: the source rows (``SELECT *`` of ``careers_department`` and
``careers_job_position``), the ``siteconfig_settings`` careers fields, and the legacy responses of
``GET /api/job-positions`` (plain and per department) and ``GET /api/job-positions/<slug>`` for every posting.
Credentials come from the libpq environment (``PGPASSWORD`` …). The database is only read (``SET default_transaction_read_only``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent


def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def fetch(url: str):
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310 - local legacy server
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--frontend-base-url", default="http://localhost:3000")
    args = parser.parse_args()

    with psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user=os.environ.get("PGUSER", "postgres"), dbname=args.db, row_factory=dict_row) as connection:
        connection.execute("SET default_transaction_read_only = on")
        departments = connection.execute("SELECT * FROM careers_department ORDER BY id").fetchall()
        positions = connection.execute("SELECT * FROM careers_job_position ORDER BY id").fetchall()
        site = connection.execute("SELECT company_name, careers_accepting_general_applications, careers_intro FROM siteconfig_settings ORDER BY id LIMIT 1").fetchone()

    base = args.url.rstrip("/")
    responses = {"list": fetch(f"{base}/api/job-positions")}
    for department in departments:
        responses[f"list?department={department['slug']}"] = fetch(f"{base}/api/job-positions?department={department['slug']}")
    for position in positions:
        responses[f"detail/{position['slug']}"] = fetch(f"{base}/api/job-positions/{position['slug']}")
    responses["detail/no-such-position"] = fetch(f"{base}/api/job-positions/no-such-position")

    payload = {
        "source": {"db": args.db, "url": base, "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")},
        "site": {**site, "frontend_base_url": args.frontend_base_url},
        "careers_department": departments,
        "careers_job_position": positions,
        "responses": {key: {"status": status, "body": body} for key, (status, body) in responses.items()},
    }
    target = HERE / f"cms_{args.scenario}.json"
    target.write_text(json.dumps(payload, indent=1, sort_keys=True, default=_json_default, ensure_ascii=False) + "\n")
    print(f"wrote {target} ({len(positions)} positions, {len(responses)} responses)")


if __name__ == "__main__":
    main()
