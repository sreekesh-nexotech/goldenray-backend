"""POST media/upload/: per-kind limits, content sniffing (spoofed types), EXIF, GPS stripping, storage placement."""

import hashlib
import io
from unittest import mock

import pytest
from PIL import Image

from audit.models import AuditLog
from media.models import MediaAsset
from media.services import assets
from media.services.storage import StorageError
from media.tests import files
from media.tests.factories import upload

pytestmark = pytest.mark.django_db
URL = "/api/v1/media/upload/"


@pytest.fixture
def uploader(make_user):
    return make_user(grants={"media": "*"})


@pytest.fixture
def client(auth_client, uploader):
    return auth_client(uploader)


def post(client, data: bytes, *, name="file.bin", content_type="application/octet-stream", **fields):
    body = {"visibility": "PUBLIC", "kind": "IMAGE", **fields, "file": upload(name, data, content_type)}
    return client.post(URL, body, format="multipart")


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        assert api_client.post(URL, {"file": upload("a.jpg", files.jpeg())}, format="multipart").status_code == 401

    def test_needs_media_create(self, auth_client, make_user):
        client = auth_client(make_user(grants={"media": ["view", "edit", "archive"]}))
        assert post(client, files.jpeg(), name="a.jpg").status_code == 403


class TestHappyPaths:
    def test_public_image(self, client, uploader, media_roots, django_capture_on_commit_callbacks):
        data = files.jpeg(size=(80, 60), captured="2025:01:02 03:04:05", offset="+05:30")
        with django_capture_on_commit_callbacks(execute=True):
            response = post(client, data, name="Roof photo.JPG", content_type="image/jpeg", alternative_text="Roof", folder="Installations/2025")
        assert response.status_code == 201, response.json()
        body = response.json()
        asset = MediaAsset.objects.get(uid=body["uid"])
        assert body["mime_type"] == "image/jpeg" and body["width"] == 80 and body["height"] == 60
        assert body["captured_at"] == "2025-01-02T03:04:05+05:30"
        assert body["checksum_sha256"] == hashlib.sha256(data).hexdigest() and body["size_bytes"] == len(data)
        assert body["original_filename"] == "Roof photo.JPG" and body["folder"] == "installations/2025"
        assert body["url"] == f"/media/public/{asset.file}" and asset.file.startswith("installations/2025/") and asset.file.endswith(".jpg")
        assert "file" not in body and "id" not in body
        assert (media_roots / "public" / asset.file).read_bytes() == data
        assert body["uploaded_by"] == str(uploader.uid)
        # the thumbnail task ran on commit
        asset.refresh_from_db()
        assert asset.thumbnail_key.endswith(".thumb.webp")
        thumb = Image.open(media_roots / "public" / asset.thumbnail_key)
        assert thumb.format == "WEBP" and max(thumb.size) <= 480
        assert AuditLog.objects.filter(action="media.asset_uploaded", object_uid=asset.uid, actor=uploader).exists()

    def test_private_document_is_never_on_the_public_side(self, client, media_roots):
        data = files.pdf()
        response = post(client, data, name="brochure.pdf", content_type="application/pdf", visibility="PRIVATE", kind="DOCUMENT")
        assert response.status_code == 201
        body = response.json()
        asset = MediaAsset.objects.get(uid=body["uid"])
        assert body["url"] is None and body["thumbnail_url"] is None and asset.cdn_url == ""
        assert (media_roots / "private" / asset.file).read_bytes() == data
        assert not (media_roots / "public").exists() or not any((media_roots / "public").rglob("*"))

    @pytest.mark.parametrize(
        "builder,name,kind,visibility,mime",
        [
            (files.png, "sig.png", "SIGNATURE", "PRIVATE", "image/png"),
            (files.webp, "a.webp", "PHOTO", "PUBLIC", "image/webp"),
            (files.heic, "IMG_0001.HEIC", "PHOTO", "PRIVATE", "image/heic"),
            (files.docx, "cv.docx", "RESUME", "PRIVATE", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            (files.doc, "cv.doc", "RESUME", "PRIVATE", "application/msword"),
            (files.pdf, "cv.pdf", "RESUME", "PRIVATE", "application/pdf"),
        ],
    )
    def test_accepted_types_per_kind(self, client, builder, name, kind, visibility, mime):
        response = post(client, builder(), name=name, kind=kind, visibility=visibility)
        assert response.status_code == 201, response.json()
        assert response.json()["mime_type"] == mime

    def test_heic_gets_a_displayable_thumbnail(self, client, media_roots, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            response = post(client, files.heic(size=(900, 600)), name="IMG.HEIC", kind="PHOTO")
        asset = MediaAsset.objects.get(uid=response.json()["uid"])
        assert Image.open(media_roots / "public" / asset.thumbnail_key).size == (480, 320)
        assert client.get(f"/api/v1/media/{asset.uid}/").json()["thumbnail_url"] == f"/media/public/{asset.thumbnail_key}"


class TestContentIsSniffedNotTrusted:
    @pytest.mark.parametrize(
        "data,name,content_type,kind,visibility",
        [
            (files.pdf(), "photo.png", "image/png", "IMAGE", "PUBLIC"),  # a PDF claiming to be a PNG
            (files.png(), "report.pdf", "application/pdf", "DOCUMENT", "PUBLIC"),  # a PNG claiming to be a PDF
            (b"<html><script>alert(1)</script></html>", "x.jpg", "image/jpeg", "IMAGE", "PUBLIC"),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "logo.svg", "image/svg+xml", "IMAGE", "PUBLIC"),
            (files.jpeg(), "sig.png", "image/png", "SIGNATURE", "PRIVATE"),  # signatures are PNG only
            (files.xls_like(), "cv.doc", "application/msword", "RESUME", "PRIVATE"),
            (files.plain_zip(), "cv.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "RESUME", "PRIVATE"),
        ],
    )
    def test_spoofed_types_are_refused(self, client, data, name, content_type, kind, visibility):
        response = post(client, data, name=name, content_type=content_type, kind=kind, visibility=visibility)
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "unsupported_file_type" and "file" in body["errors"]
        assert not MediaAsset.objects.exists()

    def test_extension_follows_the_content(self, client):
        response = post(client, files.pdf(), name="scan.png", kind="DOCUMENT", visibility="PRIVATE")
        assert response.status_code == 201
        assert MediaAsset.objects.get(uid=response.json()["uid"]).file.endswith(".pdf")


class TestLimits:
    def test_oversize_is_413_before_reading(self, client):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 1)
        response = post(client, data, name="sig.png", kind="SIGNATURE", visibility="PRIVATE")
        assert response.status_code == 413
        assert response.json()["code"] == "file_too_large" and "2 MB" in response.json()["errors"]["file"][0]

    def test_empty_file(self, client):
        response = post(client, b"", name="a.jpg")
        assert response.status_code == 400 and response.json()["code"] == "empty_file"

    def test_decompression_bomb(self, client, settings):
        settings.MEDIA_MAX_IMAGE_PIXELS = 1_000_000
        response = post(client, files.png(size=(2000, 2000), mode="1"), name="bomb.png")
        assert response.status_code == 400 and response.json()["code"] == "image_too_large"

    @pytest.mark.parametrize("kind", ["RESUME", "SIGNATURE"])
    def test_personal_data_kinds_must_be_private(self, client, kind):
        response = post(client, files.png(), name="x.png", kind=kind, visibility="PUBLIC")
        assert response.status_code == 400 and response.json()["code"] == "visibility_not_allowed"

    @pytest.mark.parametrize("folder", ["../etc", "a b", "UPPER/../x", "a//b", "x" * 121])
    def test_invalid_folder(self, client, folder):
        response = post(client, files.jpeg(), name="a.jpg", folder=folder)
        assert response.status_code == 400 and "folder" in response.json()["errors"]

    def test_reserved_folder_is_refused(self, client):
        response = post(client, files.jpeg(), name="a.jpg", folder="documents/quotation")
        assert response.status_code == 400 and response.json()["code"] == "folder_reserved"

    def test_missing_fields_use_the_error_envelope(self, client):
        response = client.post(URL, {"kind": "IMAGE"}, format="multipart")
        assert response.status_code == 400
        assert set(response.json()) == {"code", "message", "errors", "error_codes"} and {"file", "visibility"} <= set(response.json()["errors"])


class TestLocationPrivacy:
    def test_public_images_lose_their_gps_data(self, client, media_roots):
        original = files.jpeg(size=(40, 30), captured="2025:05:06 07:08:09", gps=True)
        response = post(client, original, name="site.jpg")
        body = response.json()
        stored = (media_roots / "public" / MediaAsset.objects.get(uid=body["uid"]).file).read_bytes()
        assert stored != original and body["checksum_sha256"] == hashlib.sha256(stored).hexdigest()
        exif = Image.open(io.BytesIO(stored)).getexif()
        assert 0x8825 not in exif
        assert body["captured_at"] is not None  # recorded from the original before stripping

    def test_private_images_are_stored_untouched(self, client, media_roots):
        original = files.jpeg(gps=True)
        body = post(client, original, name="site.jpg", visibility="PRIVATE", kind="PHOTO").json()
        assert (media_roots / "private" / MediaAsset.objects.get(uid=body["uid"]).file).read_bytes() == original


class TestStorageFailures:
    def test_storage_outage_is_503_and_writes_nothing(self, client):
        with mock.patch("media.services.storage.LocalStorage.save", side_effect=StorageError("disk full")):
            response = post(client, files.jpeg(), name="a.jpg")
        assert response.status_code == 503 and response.json()["code"] == "storage_unavailable"
        assert not MediaAsset.objects.exists()

    def test_row_failure_removes_the_stored_object(self, uploader, media_roots):
        with mock.patch.object(assets, "_create_row", side_effect=RuntimeError("db down")), pytest.raises(RuntimeError):
            assets.upload(user=uploader, file=upload("a.jpg", files.jpeg()), visibility="PUBLIC", kind="IMAGE")
        assert not [path for path in (media_roots / "public").rglob("*") if path.is_file()]
