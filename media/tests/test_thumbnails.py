"""Thumbnail task: idempotent, skips non-images and undecodable files, retries storage errors."""

from unittest import mock

import pytest
from celery.exceptions import Retry
from PIL import Image

from media.models import MediaAsset
from media.services import thumbnails
from media.services.storage import StorageError
from media.tasks import delete_stored_files, generate_thumbnail
from media.tests import files
from media.tests.factories import MediaAssetFactory, stored_asset

pytestmark = pytest.mark.django_db


def test_generates_once(make_user, media_roots):
    asset = stored_asset(make_user(), data=files.png(size=(1200, 300)), name="banner.png")
    key = thumbnails.generate(str(asset.uid))
    assert key and Image.open(media_roots / "public" / key).size == (480, 120)
    assert thumbnails.generate(str(asset.uid)) is None  # already done
    asset.refresh_from_db()
    assert asset.thumbnail_key == key and asset.version == 1  # derived data: no version bump


def test_skips_documents_and_deleted_assets(make_user):
    pdf = stored_asset(make_user(), data=files.pdf(), name="a.pdf", kind="DOCUMENT", visibility="PRIVATE")
    assert thumbnails.generate(str(pdf.uid)) is None
    image = stored_asset(make_user())
    image.soft_delete()
    assert thumbnails.generate(str(image.uid)) is None


def test_undecodable_original_is_skipped(make_user, media_roots):
    asset = stored_asset(make_user())
    (media_roots / "public" / asset.file).write_bytes(b"garbage")
    assert thumbnails.generate(str(asset.uid)) is None
    assert MediaAsset.objects.get(pk=asset.pk).thumbnail_key == ""


def test_storage_errors_are_retried(make_user):
    asset = MediaAssetFactory()
    with mock.patch("media.services.storage.LocalStorage.read", side_effect=StorageError("io")):
        with pytest.raises(Retry):  # autoretry_for=(StorageError,) schedules a retry instead of failing
            generate_thumbnail.apply(args=[str(asset.uid)], throw=True)


def test_delete_stored_files_task(make_user, media_roots):
    asset = stored_asset(make_user())
    assert delete_stored_files.apply(args=["PUBLIC", [asset.file, "missing/key.webp"]]).get() == 2
    assert not (media_roots / "public" / asset.file).exists()
