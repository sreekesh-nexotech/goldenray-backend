"""Byte-for-byte parity of the public delivery with the legacy CMS (PLAN §7.6 #3, "zero differences allowed").

The legacy CMS tables were exported read-only to ``fixtures/legacy_cms/tables.json`` and its responses captured to
``fixtures/legacy_cms/golden/`` (``fixtures/export_legacy_tables.py``, ``fixtures/capture_golden.py``). The same rows are
loaded through ``blog.services.legacy_import`` and every captured request must return the identical bytes — first
from the database, then from the response cache.
"""

import json
from pathlib import Path

import pytest

from blog.services import legacy_import

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_cms"
TABLES = json.loads((FIXTURES / "tables.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((FIXTURES / "golden" / "manifest.json").read_text(encoding="utf-8"))
BASE = "/api/public/v1/content/articles/"

pytestmark = pytest.mark.django_db


def golden(name: str) -> bytes:
    return (FIXTURES / "golden" / f"{name}.json").read_bytes()


@pytest.fixture
def imported():
    return legacy_import.import_all(TABLES)


def test_the_fixture_import_is_clean(imported):
    assert all(report["violations"] == [] for report in imported.values()), {table: report["violations"] for table, report in imported.items() if report["violations"]}
    assert imported["content_entry"]["created"] == 7 and imported["content_content_block"]["created"] == 14


@pytest.mark.parametrize("case", MANIFEST, ids=[case["name"] for case in MANIFEST])
def test_collection_delivery_is_byte_identical(api_client, imported, case):
    url = BASE + (f"?{case['query']}" if case["query"] else "")
    first = api_client.get(url)
    assert first.status_code == case["status"]
    assert first.content == golden(case["name"])
    cached = api_client.get(url)
    assert cached["X-Cache"] == "HIT" and cached.content == first.content


FOUND = [case for case in MANIFEST if case["name"].startswith("slug_") and json.loads(golden(case["name"]))["data"]]


@pytest.mark.parametrize("case", FOUND, ids=[case["name"] for case in FOUND])
def test_slug_route_serves_the_same_payload_as_the_slug_filter(api_client, imported, case):
    slug = case["query"].rsplit("=", 1)[1]
    response = api_client.get(f"{BASE}{slug}/")
    assert response.status_code == 200 and response.content == golden(case["name"])


def test_reimport_is_idempotent_and_keeps_parity(api_client, imported):
    again = legacy_import.import_all(TABLES)
    assert all(report["created"] == 0 and report["updated"] == 0 for report in again.values()), again
    assert api_client.get(f"{BASE}?populate=*&pagination[pageSize]=100").content == golden("list_populate_all")
