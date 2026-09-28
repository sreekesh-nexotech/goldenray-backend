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


def test_one_warm_cache_serves_every_captured_variant(api_client, imported):
    """All captured requests through one response cache, twice: queries that differ only in parameter order (sort keys,
    a repeated filter or page value — order-significant in the CMS) must never be served each other's cached body."""
    for attempt in ("MISS", "HIT"):
        for case in MANIFEST:
            response = api_client.get(BASE + (f"?{case['query']}" if case["query"] else ""))
            assert response.content == golden(case["name"]), (attempt, case["name"])
            if case["status"] == 200:
                assert response["X-Cache"] == attempt, (attempt, case["name"])


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


# ── Enriched private copy: media, attribute values, NULL texts, a category without a slug ────────────────────────────
ENRICHED = Path(__file__).parent / "fixtures" / "legacy_cms_enriched"
ENRICHED_TABLES = json.loads((ENRICHED / "tables.json").read_text(encoding="utf-8"))
ENRICHED_MANIFEST = json.loads((ENRICHED / "golden" / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def enriched():
    """The enriched tables, with the CMS media assets imported the way the media package does (``core_legacy_map``)."""
    from core.models import LegacyMap
    from media.tests.factories import MediaAssetFactory

    for row in ENRICHED_TABLES["media_asset"]:
        asset = MediaAssetFactory(cdn_url=row["cdn_url"], width=row["width"], height=row["height"], alternative_text=row["alternative_text"])
        LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id=str(row["id"]), target_table="media_asset", target_id=asset.pk)
    return legacy_import.import_all(ENRICHED_TABLES)


def test_the_enriched_import_reports_only_the_known_oddities(enriched):
    violations = sorted((table, violation["code"]) for table, report in enriched.items() for violation in report["violations"])
    assert violations == [("content_entry_attribute_value", "slot_unknown")]  # the legacy `warning` fallback key


@pytest.mark.parametrize("case", ENRICHED_MANIFEST, ids=[case["name"] for case in ENRICHED_MANIFEST])
def test_enriched_delivery_is_byte_identical(api_client, enriched, case):
    response = api_client.get(BASE + f"?{case['query']}")
    assert response.status_code == case["status"]
    assert response.content == (ENRICHED / "golden" / f"{case['name']}.json").read_bytes()
