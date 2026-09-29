"""Capture legacy CMS golden payloads and export the legacy tables (read-only) for the content-pages parity tests.

    /home/user/.venvs/platform/bin/python sitepages/tests/parity/capture_legacy.py \
        --base-url http://127.0.0.1:18009 --db legacy_blog_cms --dataset uat [--golden-only NAME]

* Routes are enumerated from the legacy database (read-only transaction): every page's ``route`` for
  ``GET /api/page-content?route=`` and every route that has FAQs (any status) for ``GET /api/faqs?page=`` — the legacy
  public FAQ endpoint takes the route in ``page`` — plus one query per ``(route, section)`` pair and two unknown routes.
* Tables are exported as JSON lists of row dicts in id order, column names as in the legacy schema. They carry no
  personal data: user references are bare ids (``accounts_admin_user`` is exported as ids only), media rows carry no
  uploader. ``siteconfig_settings`` is reduced to ``company_name`` (the only column the page payload reads).

Output: ``sitepages/tests/fixtures/legacy_cms/<dataset>/`` (pages, slots, SEO, media, settings, users,
``golden_page_content.json``) and ``faqs/tests/fixtures/legacy_cms/<dataset>/`` (categories, FAQs, ``golden_faqs.json``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]
TABLES = {
    "sitepages": {
        "sitepages_page": "SELECT * FROM sitepages_page ORDER BY id",
        "sitepages_page_seo": "SELECT * FROM sitepages_page_seo ORDER BY id",
        "sitepages_page_text_slot": "SELECT * FROM sitepages_page_text_slot ORDER BY id",
        "sitepages_page_image_slot": "SELECT * FROM sitepages_page_image_slot ORDER BY id",
        "media_asset": "SELECT id, file, cdn_url, storage_path, mime, size, width, height, alternative_text, caption, collection_id, created_at, updated_at FROM media_asset ORDER BY id",
        "siteconfig_settings": "SELECT id, company_name FROM siteconfig_settings ORDER BY id",
        "accounts_admin_user": "SELECT id FROM accounts_admin_user ORDER BY id",
    },
    "faqs": {
        "faqs_category": "SELECT * FROM faqs_category ORDER BY id",
        "faqs_faq": "SELECT * FROM faqs_faq ORDER BY id",
    },
}


def _default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(repr(value))


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=_default, sort_keys=False) + "\n")


def _get(base_url: str, path: str, params: dict) -> dict:
    url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": None}


def _rows(conn, sql: str) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def capture(base_url: str, db: str, dataset: str, golden_only: str | None = None) -> None:
    with psycopg.connect(dbname=db, user="postgres", password="postgres", host="localhost") as conn:
        conn.read_only = True
        routes = [row["route"] for row in _rows(conn, "SELECT route FROM sitepages_page ORDER BY id")]
        faq_routes = [row["route"] for row in _rows(conn, "SELECT DISTINCT p.route, p.id FROM faqs_faq f JOIN sitepages_page p ON p.id = f.page_id ORDER BY p.id")]
        sections = _rows(conn, "SELECT DISTINCT p.route, p.id, f.section FROM faqs_faq f JOIN sitepages_page p ON p.id = f.page_id ORDER BY p.id, f.section")
        exports = {app: {table: _rows(conn, sql) for table, sql in tables.items()} for app, tables in TABLES.items()}

    page_queries = [{"route": route} for route in routes] + [{"route": "/no-such-page"}]
    faq_queries = [{"page": route} for route in faq_routes] + [{"page": row["route"], "section": row["section"]} for row in sections] + [{"page": "/no-such-page"}]
    golden_pages = [{"query": query, **_get(base_url, "/api/page-content", query)} for query in page_queries]
    golden_faqs = [{"query": query, **_get(base_url, "/api/faqs", query)} for query in faq_queries]

    sitepages_dir = ROOT / "sitepages/tests/fixtures/legacy_cms" / dataset
    faqs_dir = ROOT / "faqs/tests/fixtures/legacy_cms" / dataset
    if golden_only:
        _write(sitepages_dir / f"golden_page_content_{golden_only}.json", golden_pages)
        _write(faqs_dir / f"golden_faqs_{golden_only}.json", golden_faqs)
        return
    for table, rows in exports["sitepages"].items():
        _write(sitepages_dir / f"{table}.json", rows)
    for table, rows in exports["faqs"].items():
        _write(faqs_dir / f"{table}.json", rows)
    _write(sitepages_dir / "golden_page_content.json", golden_pages)
    _write(faqs_dir / "golden_faqs.json", golden_faqs)
    print(f"{dataset}: {len(golden_pages)} page queries, {len(golden_faqs)} FAQ queries, {sum(len(r) for t in exports.values() for r in t.values())} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--golden-only", default=None, help="only capture golden payloads, suffixed with this name")
    args = parser.parse_args()
    capture(args.base_url, args.db, args.dataset, args.golden_only)
