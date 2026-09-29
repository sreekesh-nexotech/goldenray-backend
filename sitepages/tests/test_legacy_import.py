"""sitepages.services.legacy_import — idempotent, traceable import of the CMS sitepages_* tables (committed exports)."""

import copy

import pytest
from django.utils.dateparse import parse_datetime

from audit.models import AuditLog
from core.models import LegacyMap
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot
from sitepages.services import legacy_import
from sitepages.services.registry import sync_registry
from sitepages.tests import legacy_fixtures as fx

pytestmark = pytest.mark.django_db


def tables(dataset):
    return {table: fx.load("sitepages", dataset, table) for table, _ in legacy_import.IMPORTERS}


def test_uat_export_imports_every_row_with_source_values():
    results = legacy_import.import_all(tables("uat"))
    assert {table: (r["created"], r["updated"], r["skipped"], len(r["violations"])) for table, r in results.items()} == {
        "sitepages_page": (27, 0, 0, 0),
        "sitepages_page_seo": (0, 0, 0, 0),
        "sitepages_page_text_slot": (2, 0, 0, 0),
        "sitepages_page_image_slot": (1, 0, 0, 0),
    }
    source = next(row for row in fx.load("sitepages", "uat", "sitepages_page") if row["route"] == "/career")
    career = Page.objects.get(route="/career")
    assert (career.slug, career.title, career.group, career.status, career.is_protected, career.sort_order) == ("career", "Careers", "Careers", "PUBLISHED", True, source["sort_order"])
    assert career.created_at == parse_datetime(source["created_at"]) and career.updated_at == parse_datetime(source["updated_at"])
    assert LegacyMap.objects.get(source_system="CMS", source_table="sitepages_page", source_id=str(source["id"])).target_id == career.pk
    slot = PageTextSlot.objects.get(key="hero_title")
    assert (slot.page, slot.kind, slot.max_length, slot.label) == (career, "SHORT_TEXT", 90, "Hero headline")
    audit = AuditLog.objects.filter(action="sitepages.legacy_import").order_by("id").first()
    assert audit.after["source_table"] == "sitepages_page" and audit.after["created"] == 27 and len(audit.after["checksum"]) == 64


def test_rerun_skips_and_changed_rows_update():
    data = tables("uat")
    legacy_import.import_all(data)
    again = legacy_import.import_all(data)
    assert all(result["created"] == result["updated"] == 0 for result in again.values())
    assert again["sitepages_page"]["skipped"] == 27 and Page.objects.count() == 27
    changed = copy.deepcopy(data["sitepages_page"])
    row = next(row for row in changed if row["route"] == "/terms")
    row.update(name="Terms of service", status="draft", updated_at="2026-09-30T08:00:00+00:00")
    result = legacy_import.import_pages(changed)
    assert (result["created"], result["updated"], result["skipped"]) == (0, 1, 26)
    terms = Page.objects.get(route="/terms")
    assert (terms.title, terms.status, terms.version) == ("Terms of service", "DRAFT", 2)
    assert terms.updated_at == parse_datetime("2026-09-30T08:00:00+00:00")


def test_registry_pages_are_matched_by_route_not_duplicated():
    sync_registry()
    result = legacy_import.import_pages(fx.load("sitepages", "uat", "sitepages_page"))
    assert result["created"] == 0 and result["updated"] + result["skipped"] == 27
    assert Page.objects.count() == 27 and LegacyMap.objects.filter(source_table="sitepages_page").count() == 27
    slots = legacy_import.import_text_slots(fx.load("sitepages", "uat", "sitepages_page_text_slot"))
    assert slots["created"] == 0 and PageTextSlot.objects.count() == 2


def test_enriched_export_resolves_media_and_users():
    fx.seed_references("enriched")
    results = legacy_import.import_all(tables("enriched"))
    assert results["sitepages_page_seo"]["created"] == 4 and not results["sitepages_page_seo"]["violations"]
    assert results["sitepages_page_image_slot"]["created"] == 3 and results["sitepages_page_text_slot"]["created"] == 6
    career_seo = PageSeo.objects.get(page__route="/career")
    assert career_seo.og_image.cdn_url.endswith("career-og.webp") and career_seo.schema_type == "WebPage" and career_seo.updated_by is not None
    assert career_seo.schema_extra == {"inLanguage": "en-IN", "@type": "Ignored", "name": "must not override"}
    hero = PageImageSlot.objects.get(key="hero_background")
    assert hero.asset.cdn_url.endswith("career-hero.webp") and hero.alt == "Our team at work" and hero.updated_by is not None
    assert PageTextSlot.objects.get(key="cta_phone").kind == "PHONE"
    assert (Page.objects.get(route="/resources").status, Page.objects.get(route="/blog").status) == ("DRAFT", "ARCHIVED")


def test_violations_are_listed_not_raised():
    fx.seed_references("enriched")
    pages = fx.load("sitepages", "enriched", "sitepages_page")
    bad_pages = [
        {**pages[0], "id": 9001, "route": "/weird", "status": "review"},
        {**pages[0], "id": 9002, "route": "no-slash"},
        {**pages[0], "id": 9003, "route": "/career-page"},  # slug "career" is taken by /career
        {**pages[0], "id": 9004, "route": "/new-route", "created_by_id": 777},  # imported without attribution
    ]
    result = legacy_import.import_pages(pages + bad_pages)
    assert {(v["source_id"], v["code"]) for v in result["violations"]} == {("9001", "unknown_status"), ("9002", "invalid_route"), ("9003", "slug_taken"), ("9004", "unmapped_user")}
    assert result["created"] == 28 and Page.objects.get(route="/new-route").created_by is None

    seo = [{"id": 1, "page_id": 404, "schema_type": "WebPage"}, {"id": 2, "page_id": pages[1]["id"], "schema_type": "Recipe", "og_image_id": 999, "schema_extra": '{"a": 1}', "updated_at": None}]
    result = legacy_import.import_page_seo(seo)
    assert {v["code"] for v in result["violations"]} == {"unmapped_page", "unknown_schema_type", "unmapped_media"}
    saved = PageSeo.objects.get()
    assert (saved.schema_type, saved.og_image, saved.schema_extra) == ("none", None, {"a": 1})

    text = [
        {"id": 1, "page_id": pages[0]["id"], "key": "k1", "kind": "haiku"},
        {"id": 2, "page_id": pages[0]["id"], "key": "bad key", "kind": "short_text"},
        {"id": 3, "page_id": pages[0]["id"], "key": "k3", "kind": "short_text", "value": "x" * 12, "max_length": 10, "order": 1, "updated_at": "2026-09-01T00:00:00Z"},
    ]
    result = legacy_import.import_text_slots(text)
    assert [v["code"] for v in result["violations"]] == ["unknown_kind", "invalid_key", "value_out_of_rules"]
    assert PageTextSlot.objects.get(key="k3").value == "x" * 12  # kept verbatim

    images = [{"id": 1, "page_id": pages[0]["id"], "key": "bad key"}, {"id": 2, "page_id": 404, "key": "k"}]
    assert [v["code"] for v in legacy_import.import_image_slots(images)["violations"]] == ["invalid_key", "unmapped_page"]


def test_values_the_database_would_refuse_are_listed_not_raised():
    """A trailing newline passes Python's ``$`` but not the table CHECKs: the row must be skipped, not abort the run."""
    fx.seed_references("uat")
    pages = fx.load("sitepages", "uat", "sitepages_page")
    result = legacy_import.import_pages([*pages, {**pages[0], "id": 9005, "route": "/newline\n"}])
    assert [(v["source_id"], v["code"]) for v in result["violations"]] == [("9005", "invalid_route")] and result["created"] == 27
    slots = [
        {"id": 1, "page_id": pages[0]["id"], "key": "k1\n", "kind": "short_text"},
        {"id": 2, "page_id": pages[0]["id"], "key": "k2", "kind": "short_text", "value": "kept"},
    ]
    result = legacy_import.import_text_slots(slots)
    assert [v["code"] for v in result["violations"]] == ["invalid_key"] and result["created"] == 1
    images = [{"id": 1, "page_id": pages[0]["id"], "key": "hero\n"}]
    assert [v["code"] for v in legacy_import.import_image_slots(images)["violations"]] == ["invalid_key"]


def test_dry_run_writes_nothing_but_reports_exact_counts():
    result = legacy_import.import_pages(fx.load("sitepages", "uat", "sitepages_page"), dry_run=True)
    assert result["created"] == 27
    assert not Page.all_objects.exists() and not LegacyMap.objects.exists() and not AuditLog.objects.filter(action="sitepages.legacy_import").exists()
    results = legacy_import.import_all(tables("uat"), dry_run=True)
    assert results["sitepages_page_text_slot"]["created"] == 2 and not Page.all_objects.exists()


def test_timestamps_accept_datetimes_and_naive_values():
    import datetime as dt

    assert legacy_import.timestamp(dt.datetime(2026, 1, 1, 10, 0)).tzinfo is not None
    assert legacy_import.timestamp("") is None
    with pytest.raises(ValueError):
        legacy_import.timestamp("yesterday")
