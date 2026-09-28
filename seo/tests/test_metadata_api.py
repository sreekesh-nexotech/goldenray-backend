"""seo/metadata/ (staff CRUD) and the public seo/metadata/<page>/ that replaces /api/metadata/."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory
from seo.tests.factories import PageMetadataFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/seo/metadata/"
PUBLIC = "/api/public/v1/seo/metadata/"
NEW = {"page": "/About/", "title": "About Us | Flarize", "description": "Who we are.", "keywords": ["solar", "about", "solar"], "og_type": "website", "og_image_url": "/aboutHeroImg.png"}


@pytest.fixture
def editor(make_user):
    return make_user(grants={"seo": ["view", "edit"]})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


class TestStaff:
    def test_permissions(self, api_client, auth_client, make_user):
        row = PageMetadataFactory()
        assert api_client.get(URL).status_code == 401
        viewer = auth_client(make_user(grants={"seo": ["view"]}))
        assert viewer.get(URL).status_code == 200 and viewer.get(f"{URL}{row.uid}/").status_code == 200
        assert viewer.post(URL, NEW, format="json").status_code == 403
        assert viewer.patch(f"{URL}{row.uid}/", {"title": "x"}, format="json").status_code == 403
        assert viewer.delete(f"{URL}{row.uid}/").status_code == 403
        assert auth_client(make_user(grants={"blogs": "*"})).get(URL).status_code == 403

    def test_create_normalises_and_audits(self, client, editor):
        response = client.post(URL, NEW, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["page"] == "about" and body["path"] == "/about" and body["keywords"] == ["solar", "about"]
        assert AuditLog.objects.get(action="seo.page_metadata_created").actor == editor
        assert OutboxEvent.objects.get(event_type="website.revalidate_requested").payload["paths"] == ["/about"]

    def test_home_and_nested_pages(self, client):
        assert client.post(URL, {**NEW, "page": "/"}, format="json").json()["path"] == "/"
        assert client.post(URL, {**NEW, "page": "projects/123"}, format="json").json()["page"] == "projects/123"

    @pytest.mark.parametrize(("field", "value"), [("page", "a b"), ("page", "x" * 205), ("keywords", [""]), ("og_type", "Web Site"), ("title", "")])
    def test_validation(self, client, field, value):
        response = client.post(URL, {**NEW, field: value}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_page_is_unique_update_stale_and_delete(self, client):
        PageMetadataFactory(page="about")
        assert client.post(URL, NEW, format="json").json()["code"] == "page_taken"
        row = PageMetadataFactory(page="faq")
        url = f"{URL}{row.uid}/"
        updated = client.patch(url, {"title": "FAQ", "page": "faqs", "expected_version": 1}, format="json")
        assert updated.status_code == 200 and updated.json()["page"] == "faqs"
        assert OutboxEvent.objects.filter(event_type="website.revalidate_requested").last().payload["paths"] == ["/faq", "/faqs"]
        assert client.patch(url, {"title": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert client.patch(url, {"page": "about"}, format="json").json()["code"] == "page_taken"
        assert client.delete(url).status_code == 204 and client.get(url).status_code == 404

    def test_og_image_must_be_public(self, client):
        private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
        assert client.post(URL, {**NEW, "og_image_uid": str(private.uid)}, format="json").json()["code"] == "invalid_media"

    def test_list_has_no_n_plus_one(self, client, django_assert_max_num_queries):
        PageMetadataFactory.create_batch(2, og_image=MediaAssetFactory())
        with django_assert_max_num_queries(10) as small:
            client.get(URL)
        for _ in range(10):
            PageMetadataFactory(og_image=MediaAssetFactory())
        with django_assert_max_num_queries(len(small.captured_queries)):
            assert client.get(URL).json()["count"] == 12


class TestPublic:
    def test_legacy_item_shape(self, api_client):
        PageMetadataFactory(page="home", title="Golden Ray - Solar Solutions", keywords=["solar energy"], og_type="website", og_image_url="/heroImg.png")
        response = api_client.get(f"{PUBLIC}home/")
        assert response.status_code == 200
        assert response.json() == {
            "page": "home",
            "title": "Golden Ray - Solar Solutions",
            "description": "Solar solutions for Kerala homes.",
            "keywords": ["solar energy"],
            "imageUrl": "/heroImg.png",
            "ogtype": "website",
        }

    def test_nested_pages_uploaded_image_and_404(self, api_client):
        asset = MediaAssetFactory()
        PageMetadataFactory(page="projects/123", og_image=asset)
        assert api_client.get(f"{PUBLIC}projects/123/").json()["imageUrl"] == asset.cdn_url
        assert api_client.get(f"{PUBLIC}nope/").json()["code"] == "page_metadata_not_found"

    def test_cache_and_invalidation(self, api_client, client):
        row = PageMetadataFactory(page="about")
        first = api_client.get(f"{PUBLIC}about/")
        assert first["Cache-Control"] == "public, max-age=60" and api_client.get(f"{PUBLIC}about/")["X-Cache"] == "HIT"
        client.patch(f"{URL}{row.uid}/", {"title": "Changed"}, format="json")
        again = api_client.get(f"{PUBLIC}about/")
        assert again["X-Cache"] == "MISS" and again.json()["title"] == "Changed"

    def test_throttle_scope(self):
        from seo.views.public import PublicPageMetadataView

        view = PublicPageMetadataView()
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
