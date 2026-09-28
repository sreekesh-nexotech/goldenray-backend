"""media/ list, detail, PATCH, DELETE (usage-guarded), reserved folders."""

import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured

from accounts.models import User
from audit.models import AuditLog
from documents.models import RenderJob
from media import folders, usage
from media.models import MediaAsset
from media.tests import files
from media.tests.factories import MediaAssetFactory, stored_asset

pytestmark = pytest.mark.django_db
URL = "/api/v1/media/"


@pytest.fixture
def user(make_user):
    return make_user(grants={"media": "*"})


@pytest.fixture
def client(auth_client, user):
    return auth_client(user)


def referencing_job(asset, **extra):
    return RenderJob.objects.create(kind="PUBLISH_REPORT", object_type="pricing.pricerelease", object_uid=uuid.uuid4(), language="en", payload={}, payload_sha256="0" * 64, asset=asset, **extra)


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        asset = MediaAssetFactory()
        for method, path in [("get", URL), ("get", f"{URL}{asset.uid}/"), ("patch", f"{URL}{asset.uid}/"), ("delete", f"{URL}{asset.uid}/"), ("get", f"{URL}{asset.uid}/signed-url/")]:
            assert getattr(api_client, method)(path).status_code == 401, (method, path)

    @pytest.mark.parametrize("method,path,needed", [("get", "list", "view"), ("get", "detail", "view"), ("get", "signed", "view"), ("patch", "detail", "edit"), ("delete", "detail", "archive")])
    def test_each_action_needs_its_permission(self, auth_client, make_user, method, path, needed):
        asset = MediaAssetFactory()
        client = auth_client(make_user(grants={"media": [action for action in ("view", "create", "edit", "archive") if action != needed]}))
        target = {"list": URL, "detail": f"{URL}{asset.uid}/", "signed": f"{URL}{asset.uid}/signed-url/"}[path]
        assert getattr(client, method)(target, {}, format="json").status_code == 403


class TestList:
    def test_shape_filters_search(self, client):
        MediaAssetFactory(original_filename="roof.jpg", folder="installations")
        MediaAssetFactory(original_filename="brochure.pdf", kind="DOCUMENT", mime_type="application/pdf", visibility="PRIVATE")
        MediaAssetFactory(original_filename="gone.jpg").soft_delete()
        body = client.get(URL).json()
        assert body["count"] == 2
        row = next(item for item in body["results"] if item["original_filename"] == "roof.jpg")
        assert row["url"].startswith("https://cdn.example.com/") and "file" not in row and "id" not in row
        private = next(item for item in body["results"] if item["original_filename"] == "brochure.pdf")
        assert private["url"] is None
        assert [item["original_filename"] for item in client.get(URL, {"kind": "DOCUMENT"}).json()["results"]] == ["brochure.pdf"]
        assert [item["original_filename"] for item in client.get(URL, {"filter[visibility]": "PUBLIC"}).json()["results"]] == ["roof.jpg"]
        assert [item["original_filename"] for item in client.get(URL, {"folder": "installations"}).json()["results"]] == ["roof.jpg"]
        assert [item["original_filename"] for item in client.get(URL, {"search": "broch"}).json()["results"]] == ["brochure.pdf"]

    def test_reserved_folders_are_invisible(self, client):
        hidden = MediaAssetFactory(folder="documents/quotation", visibility="PRIVATE", kind="DOCUMENT")
        MediaAssetFactory(folder="documentsx")  # a different folder that merely shares the prefix text
        assert [row["folder"] for row in client.get(URL).json()["results"]] == ["documentsx"]
        for method, suffix in [("get", ""), ("patch", ""), ("delete", ""), ("get", "signed-url/")]:
            assert getattr(client, method)(f"{URL}{hidden.uid}/{suffix}", {}, format="json").status_code == 404


class TestUpdate:
    def test_alt_text_and_caption(self, client, user):
        asset = MediaAssetFactory()
        response = client.patch(f"{URL}{asset.uid}/", {"alternative_text": "Solar roof", "caption": "Kochi, 2025", "expected_version": 1}, format="json")
        assert response.status_code == 200
        assert response.json()["alternative_text"] == "Solar roof" and response.json()["version"] == 2
        entry = AuditLog.objects.get(action="media.asset_updated", object_uid=asset.uid)
        assert entry.after == {"alternative_text": "Solar roof", "caption": "Kochi, 2025"} and entry.actor == user

    def test_stale_version(self, client):
        asset = MediaAssetFactory(version=3)
        response = client.patch(f"{URL}{asset.uid}/", {"caption": "x", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_validation_envelope(self, client):
        response = client.patch(f"{URL}{MediaAssetFactory().uid}/", {"alternative_text": "x" * 300}, format="json")
        assert response.status_code == 400 and "alternative_text" in response.json()["errors"]

    def test_no_change_keeps_version(self, client):
        asset = MediaAssetFactory(caption="same")
        assert client.patch(f"{URL}{asset.uid}/", {"caption": "same"}, format="json").json()["version"] == 1


class TestDelete:
    def test_unreferenced_asset_is_soft_deleted_and_its_files_removed(self, client, user, media_roots, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            asset = stored_asset(user)
        asset.refresh_from_db()
        original, thumb = media_roots / "public" / asset.file, media_roots / "public" / asset.thumbnail_key
        assert original.exists() and thumb.exists()
        with django_capture_on_commit_callbacks(execute=True):
            response = client.delete(f"{URL}{asset.uid}/")
        assert response.status_code == 204
        assert MediaAsset.all_objects.get(pk=asset.pk).deleted_at is not None
        assert not original.exists() and not thumb.exists()
        assert AuditLog.objects.filter(action="media.asset_deleted", object_uid=asset.uid).exists()

    def test_referenced_asset_is_refused_with_the_references(self, client):
        asset = MediaAssetFactory(visibility="PRIVATE", kind="DOCUMENT", cdn_url="")
        referencing_job(asset)
        response = client.delete(f"{URL}{asset.uid}/")
        assert response.status_code == 409
        assert response.json()["code"] == "media_in_use" and response.json()["errors"]["references"] == ["documents.renderjob.asset: 1"]
        assert MediaAsset.objects.filter(pk=asset.pk).exists()

    def test_references_held_only_by_deleted_rows_do_not_block(self, client):
        asset = MediaAssetFactory()
        referencing_job(asset).soft_delete()
        assert client.delete(f"{URL}{asset.uid}/").status_code == 204

    def test_stale_version(self, client):
        asset = MediaAssetFactory(version=2)
        assert client.delete(f"{URL}{asset.uid}/?expected_version=1").status_code == 409


class TestRegistries:
    def test_usage_only_accepts_relations_to_media(self):
        with pytest.raises(ImproperlyConfigured):
            usage.register(User, "email")
        with pytest.raises(ImproperlyConfigured):
            usage.register(User, "nope")
        assert "documents.renderjob.asset" in usage.registered()

    def test_references_report_live_and_deleted(self):
        asset = MediaAssetFactory()
        referencing_job(asset)
        referencing_job(asset).soft_delete()
        [reference] = usage.references(asset)
        assert (reference.label, reference.live, reference.deleted) == ("documents.renderjob.asset", 1, 1)

    def test_folder_validation_and_reservation(self):
        assert folders.validate(" /Brand/Logos/ ") == "brand/logos"
        with pytest.raises(ValueError):
            folders.validate("a/../b")
        with pytest.raises(ValueError):
            folders.reserve("")
        folders.reserve("hr-test")
        try:
            assert folders.is_reserved("hr-test/photos") and not folders.is_reserved("hr-testing")
        finally:
            folders.release("hr-test")
        assert "documents" in folders.reserved()


def test_signed_url_for_a_public_asset_is_its_cdn_url(client):
    asset = MediaAssetFactory()
    body = client.get(f"{URL}{asset.uid}/signed-url/").json()
    assert body == {"url": asset.cdn_url, "thumbnail_url": None, "expires_at": None}


def test_generated_bytes_share_the_upload_validation(user):
    from core.errors import DomainError
    from media.services.assets import store_bytes

    asset = store_bytes(user=user, data=files.pdf(), filename="r.pdf", kind="DOCUMENT", visibility="PRIVATE", folder="documents/test")
    assert asset.folder == "documents/test" and asset.mime_type == "application/pdf"
    with pytest.raises(DomainError) as excinfo:
        store_bytes(user=user, data=b"not a pdf", filename="r.pdf", kind="DOCUMENT", visibility="PRIVATE", folder="documents/test")
    assert excinfo.value.code == "unsupported_file_type"


def test_listing_cost_does_not_grow_with_rows(client, django_assert_max_num_queries):
    for _ in range(3):
        MediaAssetFactory(thumbnail_key="library/2026/09/x.thumb.webp")
    client.get(URL)  # warm the grants/session caches
    with django_assert_max_num_queries(6) as few:
        client.get(URL)
    for _ in range(20):
        MediaAssetFactory(thumbnail_key="library/2026/09/x.thumb.webp")
    with django_assert_max_num_queries(len(few.captured_queries)):
        assert client.get(URL, {"page_size": 50}).json()["count"] == 23
