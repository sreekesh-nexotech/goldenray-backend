"""blog.services.legacy_import: idempotency, traceability, attribution/media maps and every reported violation."""

import copy
import json
from pathlib import Path

import pytest

from audit.models import AuditLog
from blog.models import Author, Category, Collection, ContentBlock, Entry, EntryAttributeValue, EntryImage, EntrySeo, EntrySlugHistory, Tag, Template, TemplateAttributeSlot, TemplateImageGroup
from blog.services import legacy_import
from blog.tests.factories import EntryFactory
from core.models import LegacyMap
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
TABLES = json.loads((Path(__file__).parent / "fixtures" / "legacy_cms" / "tables.json").read_text(encoding="utf-8"))


def tables(**overrides):
    data = copy.deepcopy(TABLES)
    data.update(overrides)
    return data


def codes(report):
    return [violation["code"] for violation in report["violations"]]


def test_full_import_maps_every_row_and_preserves_identity():
    results = legacy_import.import_all(TABLES)
    for table, rows in TABLES.items():
        if table in legacy_import.TABLE_ORDER:
            assert LegacyMap.objects.filter(source_system="CMS", source_table=table).count() == len(rows), table
    entry = Entry.objects.get(slug="how-net-metering-works")
    assert str(entry.uid) == "3c41703e-65e1-4381-b354-b87b4913eb9f" and entry.delivery_id == 3
    assert entry.updated_at.isoformat() == "2026-09-28T17:43:51.388951+00:00" and entry.status == Entry.Status.PUBLISHED
    archived = Entry.objects.get(slug="archived-old-subsidy")
    assert archived.status == Entry.Status.ARCHIVED and archived.archived_at is not None
    assert Collection.objects.get().path_prefix == "/blog" and TemplateAttributeSlot.objects.get().type == "ENUM"
    assert Author.objects.get(name="Anu Thomas").slug == "anu-thomas" and Tag.objects.get(name="Kerala").delivery_id == 2
    assert EntrySlugHistory.objects.get().slug == "net-metering-explained" and EntrySeo.objects.count() == 7
    assert results["content_entry_image"]["created"] == 20 and ContentBlock.objects.count() == 14
    assert AuditLog.objects.filter(action="blog.legacy_import").count() == len(legacy_import.TABLE_ORDER)  # one row per table, empty ones included
    log = AuditLog.objects.filter(action="blog.legacy_import", after__table="content_entry").get()
    assert log.after["created"] == 7 and len(log.after["checksum"]) == 64


def test_reimport_updates_changed_rows_and_never_duplicates():
    legacy_import.import_all(TABLES)
    changed = tables()
    changed["content_entry"][0]["title"] = "Updated title"
    changed["content_entry_tags"] = [row for row in changed["content_entry_tags"] if row["id"] != 2]
    results = legacy_import.import_all(changed)
    assert results["content_entry"] == {"created": 0, "updated": 1, "skipped": 6, "violations": []}
    assert results["content_entry_tags"]["updated"] == 1  # the link that disappeared in the source is removed
    assert Entry.objects.count() == 7 and Entry.objects.get(delivery_id=1).title == "Updated title"


def test_dry_run_writes_nothing():
    results = legacy_import.import_all(TABLES, dry_run=True)
    assert results["content_entry"]["created"] == 7 and not Entry.objects.exists() and not LegacyMap.objects.exists()


def test_user_map_attribution_and_unmapped_users(make_user):
    user = make_user()
    data = tables()
    data["content_entry"][0]["created_by_id"] = 11
    data["content_entry"][0]["updated_by_id"] = 12
    report = legacy_import.import_all(data, user_map={11: user})["content_entry"]
    entry = Entry.objects.get(delivery_id=1)
    assert entry.created_by == user and entry.updated_by is None
    assert codes(report) == ["user_unmapped"]


def test_media_references_resolve_through_the_media_import_map():
    asset = MediaAssetFactory()
    LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id="5", target_table="media_asset", target_id=asset.pk)
    data = tables()
    data["content_entry"][0]["cover_image_id"] = 5
    data["content_entry"][1]["cover_image_id"] = 6  # never imported
    data["content_entry_image"][0].update(external_url="", media_asset_id=5)
    data["content_entry_image"][1].update(external_url="", media_asset_id=6)
    data["content_entry_image"][2].update(media_asset_id=5)  # both sources: the URL (what the CMS delivered) wins
    results = legacy_import.import_all(data)
    assert Entry.objects.get(delivery_id=1).cover_image == asset and Entry.objects.get(delivery_id=2).cover_image is None
    assert codes(results["content_entry"]) == ["media_unmapped"]
    assert sorted(codes(results["content_entry_image"])) == ["image_without_source", "media_unmapped", "two_sources"]
    assert EntryImage.objects.filter(media_asset=asset).count() == 1 and EntryImage.objects.count() == 19


def test_explicit_media_map_and_private_assets():
    public, private = MediaAssetFactory(), MediaAssetFactory(visibility="PRIVATE", cdn_url="")
    data = tables()
    data["content_entry"][0]["cover_image_id"] = 1
    data["content_entry"][1]["cover_image_id"] = 2
    report = legacy_import.import_all(data, media_map={1: public, "2": private})["content_entry"]
    assert Entry.objects.get(delivery_id=1).cover_image == public and codes(report) == ["media_not_public"]


def test_entry_violations():
    data = tables()
    rows = data["content_entry"]
    rows[0]["status"] = "weird"
    rows[1]["status"] = "deleted"
    rows[1]["deleted_at"] = "2026-06-01T00:00:00+00:00"
    rows[2]["slug"] = "test-2"
    rows[3]["published_at"] = None
    rows[4]["read_time"] = 99999
    rows[5]["template_id"] = 99
    rows[6]["author_id"] = 99
    rows.append({**rows[0], "id": 50, "document_id": "00000000-0000-0000-0000-000000000050", "slug": rows[1]["slug"], "status": "draft"})
    rows.append({**rows[0], "id": 51, "document_id": "00000000-0000-0000-0000-000000000051", "slug": "orphan-entry", "collection_id": 99})
    report = legacy_import.import_all(data)["content_entry"]
    assert sorted(codes(report)) == sorted(
        ["status_unknown", "slug_invalid", "published_at_missing", "read_time_out_of_range", "template_unmapped", "author_unmapped", "slug_clash", "collection_unmapped"]
    )
    assert Entry.objects.get(delivery_id=1).status == Entry.Status.DRAFT
    deleted = Entry.objects.get(delivery_id=2)
    assert deleted.status == Entry.Status.ARCHIVED and deleted.archived_at.isoformat() == "2026-06-01T00:00:00+00:00"
    assert Entry.objects.get(delivery_id=3).slug == "test-2"  # a live URL is never rewritten
    assert Entry.objects.get(delivery_id=4).published_at is not None and Entry.objects.get(delivery_id=5).read_time is None
    assert report["skipped"] == 2


def test_taxonomy_and_schema_violations():
    data = tables()
    data["catalog_collection"].append({**data["catalog_collection"][0], "id": 2, "api_uid": "preview"})
    data["catalog_template_image_group"][0]["max_items"] = 3
    data["catalog_template_image_group"].append({**data["catalog_template_image_group"][0], "id": 9, "key": "orphan", "template_id": 42})
    data["catalog_template_attribute_slot"].append({**data["catalog_template_attribute_slot"][0], "id": 2, "key": "mystery", "type": "colour", "options": {}})
    data["catalog_template_attribute_slot"].append({**data["catalog_template_attribute_slot"][0], "id": 3, "key": "bad", "type": "enum", "options": {}})
    data["catalog_template_attribute_slot"].append({**data["catalog_template_attribute_slot"][0], "id": 4, "key": "gone", "template_id": 42})
    data["catalog_category"].append({"id": 9, "name": "No Slug", "slug": None})
    data["catalog_badge"][0]["color"] = "orange"
    results = legacy_import.import_all(data)
    assert codes(results["catalog_collection"]) == ["api_uid_invalid"]
    assert sorted(codes(results["catalog_template_image_group"])) == ["max_items_dropped", "template_unmapped"]
    assert sorted(codes(results["catalog_template_attribute_slot"])) == ["options_invalid", "template_unmapped", "type_unknown"]
    assert codes(results["catalog_category"]) == ["slug_generated"] and Category.objects.get(name="No Slug").slug == "no-slug"
    assert codes(results["catalog_badge"]) == ["color_invalid"]
    assert TemplateImageGroup.objects.get(key="coverImg").max_items is None
    assert TemplateAttributeSlot.objects.get(key="bad").options == {} and Template.objects.count() == 1


def test_children_violations_and_coercion():
    data = tables()
    data["content_entry_attribute_value"] = [
        {"id": 1, "entry_id": 1, "slot_key": "difficulty", "value": "Beginner"},
        {"id": 2, "entry_id": 1, "slot_key": "unknown", "value": 3},
        {"id": 3, "entry_id": 2, "slot_key": "difficulty", "value": "Expert"},
        {"id": 4, "entry_id": 404, "slot_key": "difficulty", "value": "Beginner"},
    ]
    data["content_content_block"].append({"id": 99, "entry_id": 1, "component": "custom.widget", "body": [], "order": 3})
    data["content_content_block"].append({"id": 98, "entry_id": 1, "component": "Bad Name", "body": [], "order": 4})
    data["content_entry_slug_history"].append(
        {"id": 2, "entry_id": 1, "collection_id": 1, "slug": "on-grid-vs-hybrid-kerala", "is_active": True, "note": "", "created_at": "2026-09-28T17:43:51+00:00"}
    )
    data["content_entry_categories"].append({"id": 99, "entry_id": 1, "category_id": 404})
    data["content_seo"].append({"id": 99, "entry_id": 404, "meta_title": "x", "meta_description": None, "canonical_url": None, "keywords": None})
    results = legacy_import.import_all(data)
    assert sorted(codes(results["content_entry_attribute_value"])) == ["entry_unmapped", "slot_unknown", "value_invalid"]
    assert EntryAttributeValue.objects.count() == 3  # values that do not fit are kept verbatim and reported
    assert sorted(codes(results["content_content_block"])) == ["component_malformed", "component_unknown", "component_unknown"]
    assert ContentBlock.objects.get(delivery_id=99).component == "custom.widget"
    assert codes(results["content_entry_slug_history"]) == ["alias_clash"] and not EntrySlugHistory.objects.get(slug="on-grid-vs-hybrid-kerala").active
    assert codes(results["content_entry_categories"]) == ["link_unmapped"]
    assert codes(results["content_seo"]) == ["entry_unmapped"]


def test_delivery_id_collision_assigns_a_new_number():
    EntryFactory(delivery_id=1, slug="native-entry")
    report = legacy_import.import_all(tables())["content_entry"]
    assert codes(report) == ["delivery_id_taken"]
    imported = Entry.objects.get(uid="e6748356-b991-4ece-8c3a-146b9c03ecf4")
    assert imported.delivery_id not in (1, *range(2, 8))


def test_seo_block_adopts_an_existing_row():
    legacy_import.import_all(TABLES)
    LegacyMap.objects.filter(source_table="content_seo").delete()
    report = legacy_import.import_entry_seo(TABLES["content_seo"])
    assert report["created"] == 0 and EntrySeo.all_objects.count() == 7
