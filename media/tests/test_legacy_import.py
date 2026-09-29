"""CMS ``media_asset`` → ``media_asset`` (PLAN §7.2 row 3). The seeded CMS has no media, so the rows are synthetic in the
CMS table's exact shape (``migrations_tools/tests/fixtures`` holds the column list through the other tables)."""

import hashlib

import pytest

from audit.models import AuditLog
from core.models import LegacyMap
from media.models import MediaAsset
from media.services import legacy_import
from media.services.storage import StorageError
from media.tests.files import pdf, webp

pytestmark = pytest.mark.django_db


def cms_row(**overrides):
    row = {
        "id": 7,
        "file": "uploads/2025/01/hero.webp",
        "storage_path": "uploads/2025/01/hero.webp",
        "cdn_url": "https://golden-ray.b-cdn.net/uploads/2025/01/hero.webp",
        "mime": "image/webp",
        "size": 1234,
        "width": 1200,
        "height": 630,
        "alternative_text": "Rooftop array",
        "caption": "",
        "collection_id": 1,
        "created_at": "2025-01-02T03:04:05+00:00",
        "updated_at": "2025-01-03T03:04:05+00:00",
    }
    return {**row, **overrides}


def asset():
    return MediaAsset.all_objects.get(pk=LegacyMap.objects.get(source_table="media_asset").target_id)


def test_bunny_url_kept_folder_from_collection_timestamps_preserved():
    result = legacy_import.import_cms_assets([cms_row()], collections={1: "articles"})
    item = asset()
    assert result["created"] == 1 and item.is_public and item.kind == "IMAGE"
    assert item.cdn_url == cms_row()["cdn_url"] and item.file == "uploads/2025/01/hero.webp" and item.folder == "articles"
    assert item.created_at.isoformat() == "2025-01-02T03:04:05+00:00" and item.size_bytes == 1234 and item.checksum_sha256 == ""
    assert [violation["code"] for violation in result["violations"]] == ["checksum_unknown"]
    assert AuditLog.objects.filter(action="media.legacy_import").count() == 1


def test_checksum_from_the_volume_and_size_mismatch_listed():
    data = webp()
    result = legacy_import.import_cms_assets([cms_row(size=1)], read_file=lambda path: data if path == "uploads/2025/01/hero.webp" else None)
    assert asset().checksum_sha256 == hashlib.sha256(data).hexdigest() and asset().size_bytes == len(data)
    assert [violation["code"] for violation in result["violations"]] == ["size_mismatch"]


def test_uploads_only_file_is_reuploaded_to_public_storage(settings):
    data = webp()
    result = legacy_import.import_cms_assets([cms_row(cdn_url="", storage_path="", file="/uploads/x/../a.webp")], read_file=lambda path: data)
    item = asset()
    assert result["created"] == 1 and item.file == "uploads/x/a.webp" and item.cdn_url == "/media/public/uploads/x/a.webp"
    assert (settings.PUBLIC_MEDIA_ROOT / "uploads/x/a.webp").read_bytes() == data
    again = legacy_import.import_cms_assets([cms_row(cdn_url="", storage_path="", file="/uploads/x/../a.webp")], read_file=lambda path: data)
    assert again["skipped"] == 1 and asset().cdn_url == item.cdn_url


def test_uploads_only_file_dry_run_uploads_nothing(settings):
    result = legacy_import.import_cms_assets([cms_row(cdn_url="")], read_file=lambda path: webp(), dry_run=True)
    assert result["created"] == 1 and "reupload_pending" in [violation["code"] for violation in result["violations"]]
    assert not MediaAsset.all_objects.exists() and not (settings.PUBLIC_MEDIA_ROOT / "uploads").exists()


def test_unavailable_or_unstorable_files_are_refused(monkeypatch):
    missing = legacy_import.import_cms_assets([cms_row(cdn_url="")])
    assert missing["skipped"] == 1 and missing["violations"][0]["code"] == "file_unavailable"
    no_path = legacy_import.import_cms_assets([cms_row(file="", storage_path="")])
    assert no_path["violations"][0]["code"] == "file_missing"

    class Broken:
        def save(self, key, data):
            raise StorageError("down")

    monkeypatch.setattr(legacy_import, "storage_for", lambda visibility: Broken())
    broken = legacy_import.import_cms_assets([cms_row(cdn_url="")], read_file=lambda path: b"x")
    assert broken["violations"][0]["code"] == "reupload_failed" and not MediaAsset.all_objects.exists()


def test_rerun_is_idempotent_updates_metadata_and_keeps_deleted_rows_deleted():
    legacy_import.import_cms_assets([cms_row()])
    assert legacy_import.import_cms_assets([cms_row()])["skipped"] == 1
    assert legacy_import.import_cms_assets([cms_row(alternative_text="New alt")])["updated"] == 1
    assert asset().alternative_text == "New alt" and asset().version == 2
    MediaAsset.all_objects.filter(pk=asset().pk).update(deleted_at=asset().created_at)
    assert legacy_import.import_cms_assets([cms_row(alternative_text="Again")])["skipped"] == 1
    assert MediaAsset.all_objects.count() == 1


def test_pdf_kind_bad_folder_and_storage_key_clash():
    result = legacy_import.import_cms_assets(
        [cms_row(id=1, mime="application/pdf", file="docs/a.pdf", storage_path="docs/a.pdf", collection_id=2), cms_row(id=2, file="docs/a.pdf", storage_path="docs/a.pdf")],
        collections={2: "Bad Name!"},
        read_file=lambda path: pdf(),
    )
    first = MediaAsset.all_objects.get()
    assert first.kind == "DOCUMENT" and first.folder == "cms"
    assert [violation["code"] for violation in result["violations"]] == ["size_mismatch", "folder_invalid", "size_mismatch", "storage_key_taken"]


def test_reupload_never_overwrites_a_file_another_asset_owns(settings):
    ours = webp()
    (settings.PUBLIC_MEDIA_ROOT / "uploads").mkdir(parents=True)
    (settings.PUBLIC_MEDIA_ROOT / "uploads/logo.webp").write_bytes(ours)
    MediaAsset.objects.create(
        visibility="PUBLIC", kind="IMAGE", file="uploads/logo.webp", cdn_url="/media/public/uploads/logo.webp", original_filename="logo.webp", mime_type="image/webp", size_bytes=len(ours)
    )
    result = legacy_import.import_cms_assets([cms_row(cdn_url="", storage_path="", file="uploads/logo.webp")], read_file=lambda path: b"legacy bytes")
    assert [violation["code"] for violation in result["violations"]] == ["storage_key_taken"]
    assert (settings.PUBLIC_MEDIA_ROOT / "uploads/logo.webp").read_bytes() == ours  # the platform's file is untouched
    assert result["skipped"] == 1 and not LegacyMap.objects.exists()
