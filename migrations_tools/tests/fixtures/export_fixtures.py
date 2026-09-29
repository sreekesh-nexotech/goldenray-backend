"""Export the committed fixtures ``cms.json`` / ``backend.json`` from restored copies of the UAT legacy dumps (read-only).

    DJANGO_SETTINGS_MODULE=flarize.settings.test python migrations_tools/tests/fixtures/export_fixtures.py \\
        --cms-url postgresql://postgres:postgres@localhost/<private copy of legacy_blog_cms> \\
        --backend-url postgresql://postgres:postgres@localhost/<private copy of legacy_goldenapp>

Every table of both import plans is exported (``SELECT *`` through the read-only source), as the importers receive
it, plus the non-empty tables the plans list as not migrated (so the row-count check sees them). Two reductions keep
the files small: ``pincodes`` keeps the post offices of the first 30 pincodes plus every pincode an installation uses,
and the framework tables (``auth_permission``, ``django_*``) keep their first 3 rows. Personal data is masked:
installation customers become ``Customer <id>`` with a synthetic phone and address.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import django

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))


def encode(value):
    """JSON for a legacy value; date-times keep their microseconds (DjangoJSONEncoder would cut them to milliseconds,
    and the delivery payloads print them in full)."""
    import datetime as dt
    import uuid
    from decimal import Decimal

    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def mask(table: str, row: dict) -> dict:
    if table == "customer_installations":
        row = {**row, "customer_name": f"Customer {row['id']}", "phone_number": f"+9190000{int(row['id']):05d}", "address": f"House {row['id']}, Alappuzha"}
    return row


def export(source, plan) -> dict:
    tables = {}
    for table in plan.tables:
        tables[table] = [mask(table, row) for row in source.rows(table)]
    for table in plan.not_migrated:
        if source.count(table):
            tables[table] = source.rows(table)[:3]
    if "pincodes" in tables:
        used = {str(row.get("pincode")) for row in tables.get("customer_installations", [])}
        first = []
        for row in tables["pincodes"]:
            if str(row["pincode"]) not in first and len(first) < 30:
                first.append(str(row["pincode"]))
        keep = used | set(first)
        tables["pincodes"] = [row for row in tables["pincodes"] if str(row["pincode"]) in keep]
    return tables


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cms-url", required=True)
    parser.add_argument("--backend-url", required=True)
    args = parser.parse_args()
    django.setup()

    from migrations_tools.services import backend, cms
    from migrations_tools.services.source import PostgresSource

    for name, url, plan in (("cms", args.cms_url, cms.PLAN), ("backend", args.backend_url, backend.PLAN)):
        with PostgresSource(url) as source:
            tables = export(source, plan)
        (HERE / f"{name}.json").write_text(json.dumps({"tables": tables}, default=encode, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        print(name, sum(len(rows) for rows in tables.values()), "rows")


if __name__ == "__main__":
    main()
