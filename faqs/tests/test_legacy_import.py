"""faqs.services.legacy_import — idempotent, traceable import of the CMS faqs_category / faqs_faq tables."""

import copy

import pytest
from django.utils.dateparse import parse_datetime

from audit.models import AuditLog
from core.models import LegacyMap
from faqs.models import Faq, FaqCategory
from faqs.services import legacy_import
from sitepages.tests import legacy_fixtures as fx

pytestmark = pytest.mark.django_db


@pytest.fixture
def enriched_pages():
    fx.seed_references("enriched")
    fx.import_pages("enriched")


def test_enriched_export_imports_every_row(enriched_pages):
    categories = fx.load("faqs", "enriched", "faqs_category")
    faqs = fx.load("faqs", "enriched", "faqs_faq")
    results = legacy_import.import_all({"faqs_category": categories, "faqs_faq": faqs})
    assert (results["faqs_category"]["created"], results["faqs_faq"]["created"]) == (3, 106)
    assert [v["code"] for v in results["faqs_faq"]["violations"]] == ["published_incomplete"]  # the blank-answer FAQ, kept published
    source = next(row for row in faqs if row["id"] == 73)
    faq = Faq.objects.get(pk=LegacyMap.objects.get(source_table="faqs_faq", source_id="73").target_id)
    assert (faq.question, faq.answer, faq.sort_order, faq.status, faq.page.route, faq.category.slug) == (source["question"], source["answer"], 0, "PUBLISHED", "/emi-calculator", "financing")
    assert faq.published_at == parse_datetime(source["published_at"]) and faq.created_at == parse_datetime(source["created_at"])
    seo = Faq.objects.get(pk=LegacyMap.objects.get(source_table="faqs_faq", source_id="60").target_id)
    assert (seo.seo_title, seo.noindex, seo.schema_extra) == ("Residential FAQ", True, {"k": "v"})
    assert Faq.objects.get(pk=LegacyMap.objects.get(source_table="faqs_faq", source_id="25").target_id).created_by is not None
    assert FaqCategory.objects.get(slug="financing").is_active is False
    assert AuditLog.objects.filter(action="faqs.legacy_import").count() == 2


def test_rerun_is_idempotent_and_changes_update(enriched_pages):
    data = {"faqs_category": fx.load("faqs", "enriched", "faqs_category"), "faqs_faq": fx.load("faqs", "enriched", "faqs_faq")}
    legacy_import.import_all(data)
    again = legacy_import.import_all(data)
    assert again["faqs_faq"]["skipped"] == 106 and again["faqs_category"]["skipped"] == 3 and Faq.objects.count() == 106
    changed = copy.deepcopy(data["faqs_faq"])
    changed[0].update(question="Changed?", display_order=9)
    result = legacy_import.import_faqs(changed)
    assert (result["created"], result["updated"]) == (0, 1)
    faq = Faq.objects.get(question="Changed?")
    assert (faq.sort_order, faq.version) == (9, 2)


def test_categories_match_existing_slugs(enriched_pages):
    from faqs.tests.factories import FaqCategoryFactory

    existing = FaqCategoryFactory(name="Subsidy", slug="subsidy")
    result = legacy_import.import_categories(fx.load("faqs", "enriched", "faqs_category"))
    assert result["created"] == 2 and result["updated"] == 1
    assert LegacyMap.objects.get(source_table="faqs_category", source_id="1").target_id == existing.pk


def test_violations(enriched_pages):
    from faqs.tests.factories import FaqCategoryFactory

    FaqCategoryFactory(name="Taken", slug="taken-elsewhere")
    categories = [{"id": 1, "name": "", "slug": "x"}, {"id": 2, "name": "Taken", "slug": "another"}]
    assert [v["code"] for v in legacy_import.import_categories(categories)["violations"]] == ["incomplete_category", "category_taken"]
    base = fx.load("faqs", "enriched", "faqs_faq")[0]
    rows = [
        {**base, "id": 9001, "status": "review"},
        {**base, "id": 9002, "page_id": 4040},
        {**base, "id": 9003, "category_id": 55, "og_image_id": 77, "schema_type": "Recipe", "schema_extra": '{"a": 1}', "updated_by_id": 666},
    ]
    result = legacy_import.import_faqs(rows)
    assert [(v["source_id"], v["code"]) for v in result["violations"]] == [
        ("9001", "unknown_status"),
        ("9002", "unmapped_page"),
        ("9003", "unmapped_category"),
        ("9003", "unknown_schema_type"),
        ("9003", "unmapped_media"),
        ("9003", "unmapped_user"),
    ]
    faq = Faq.objects.get()
    assert (faq.category, faq.og_image, faq.schema_type, faq.schema_extra, faq.updated_by) == (None, None, "none", {"a": 1}, None)


def test_dry_run_writes_nothing(enriched_pages):
    results = legacy_import.import_all({"faqs_category": fx.load("faqs", "enriched", "faqs_category"), "faqs_faq": fx.load("faqs", "enriched", "faqs_faq")}, dry_run=True)
    assert results["faqs_faq"]["created"] == 106
    assert not Faq.all_objects.exists() and not FaqCategory.all_objects.exists() and not LegacyMap.objects.filter(source_table__startswith="faqs_").exists()
    assert legacy_import.import_faqs(fx.load("faqs", "enriched", "faqs_faq"), dry_run=True)["created"] == 106 and not Faq.all_objects.exists()
