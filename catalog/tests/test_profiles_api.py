"""catalog/public-profiles/ — CRUD, publish/unpublish (module products_public)."""

import pytest

from audit.models import AuditLog
from catalog.models import ComponentPublicProfile, ComponentStatus
from catalog.tests.factories import BrandFactory, PublicProfileFactory, panel, published
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/public-profiles/"


@pytest.fixture
def editor(auth_client, make_user):
    return auth_client(make_user(grants={"products_public": "*"}))


def detail(profile, suffix=""):
    return f"{URL}{profile.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_forbidden_and_view_only(self, api_client, auth_client, make_user, client):
        profile = PublicProfileFactory()
        assert api_client.get(URL).status_code == 401
        assert client.get(URL).status_code == 403  # catalog grants do not include products_public
        viewer = auth_client(make_user(grants={"products_public": ["view"]}))
        assert viewer.get(detail(profile)).status_code == 200
        assert viewer.post(URL, {"component_uid": str(profile.component.uid)}, format="json").status_code == 403
        assert viewer.patch(detail(profile), {"headline": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(profile)).status_code == 403
        assert viewer.post(detail(profile, "publish/")).status_code == 403
        editor_only = auth_client(make_user(grants={"products_public": ["view", "edit"]}))
        assert editor_only.post(detail(profile, "publish/")).status_code == 403

    def test_scope_all(self, auth_client, make_user):
        PublicProfileFactory.create_batch(2)
        client = auth_client(make_user(grants={"products_public": ["view"]}, scopes={"products_public": "all"}))
        assert client.get(URL).json()["count"] == 2


class TestCrud:
    def test_create_with_derived_slug(self, editor):
        component = panel(brand=BrandFactory(name="Waaree"), brand_label="Waaree", model="Ahnay Bi-55-550")
        response = editor.post(
            URL,
            {
                "component_uid": str(component.uid),
                "headline": "Ahnay Bi-55-550",
                "ratings": {"efficiency": 93, "kerala_climate": 96},
                "overall_rating": "EXCELLENT",
                "pros": ["Dual glass"],
                "faq": [{"question": "DCR?", "answer": "No."}],
                "gallery": [str(MediaAssetFactory().uid)],
            },
            format="json",
        )
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["slug"] == "waaree-ahnay-bi-55-550" and body["status"] == "DRAFT" and body["component"]["sku"] == component.sku and body["ratings"] == {"efficiency": 93, "kerala_climate": 96}
        assert editor.post(URL, {"component_uid": str(component.uid)}, format="json").json()["code"] == "profile_exists"

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"ratings": {"looks": 5}}, "ratings"),
            ({"ratings": {"efficiency": 101}}, "ratings"),
            ({"faq": [{"question": "q"}]}, "faq"),
            ({"gallery": ["not-a-uuid"]}, "gallery"),
            ({"kerala_climate_score": 120}, "kerala_climate_score"),
            ({"overall_rating": "SUPERB"}, "overall_rating"),
            ({"slug": "Bad Slug"}, "slug"),
        ],
    )
    def test_validation(self, editor, payload, field):
        component = panel()
        response = editor.post(URL, {"component_uid": str(component.uid), **payload}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"], response.json()

    def test_gallery_must_be_public_images(self, editor):
        component = panel()
        private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
        response = editor.post(URL, {"component_uid": str(component.uid), "gallery": [str(private.uid)]}, format="json")
        assert response.status_code == 400 and "gallery" in response.json()["errors"]

    def test_slug_conflict_update_stale_and_delete_restore(self, editor):
        PublicProfileFactory(slug="taken")
        profile = PublicProfileFactory(slug="mine")
        assert editor.patch(detail(profile), {"slug": "taken"}, format="json").json()["code"] == "profile_slug_taken"
        assert editor.patch(detail(profile), {"headline": "New", "expected_version": 1}, format="json").json()["version"] == 2
        assert editor.patch(detail(profile), {"headline": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        component = profile.component
        assert editor.delete(detail(profile)).status_code == 204
        recreated = editor.post(URL, {"component_uid": str(component.uid), "slug": "mine"}, format="json")
        assert recreated.status_code == 201 and recreated.json()["uid"] == str(profile.uid) and recreated.json()["headline"] == ""
        assert ComponentPublicProfile.all_objects.filter(component=component).count() == 1


class TestPublish:
    def test_publish_and_unpublish(self, editor):
        profile = PublicProfileFactory()
        response = editor.post(detail(profile, "publish/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "PUBLISHED" and response.json()["published_at"]
        assert editor.post(detail(profile, "publish/")).json()["version"] == 2  # idempotent
        assert OutboxEvent.objects.get(event_type="catalog.profile_published").payload["slug"] == profile.slug
        assert editor.post(detail(profile, "unpublish/")).json()["status"] == "DRAFT"
        assert editor.post(detail(profile, "unpublish/")).json()["status"] == "DRAFT"
        assert AuditLog.objects.filter(action__in=["catalog.profile_published", "catalog.profile_unpublished"]).count() == 2
        assert OutboxEvent.objects.filter(event_type="catalog.profile_unpublished").count() == 1

    @pytest.mark.parametrize("change", [{"is_public": False}, {"status": ComponentStatus.RETIRED}, {"status": ComponentStatus.DRAFT}])
    def test_component_must_be_publishable(self, editor, change):
        profile = PublicProfileFactory()
        type(profile.component).objects.filter(pk=profile.component_id).update(**change)
        response = editor.post(detail(profile, "publish/"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "component_not_publishable"

    @pytest.mark.parametrize("change", [{"is_active": False}, {"deleted_at": "2026-01-01T00:00:00Z"}])
    def test_category_must_be_live_and_active(self, editor, api_client, change):
        """A profile whose category is hidden from the website must not report PUBLISHED (the public lists drop it)."""
        profile = PublicProfileFactory()
        type(profile.component.category).all_objects.filter(pk=profile.component.category_id).update(**change)
        response = editor.post(detail(profile, "publish/"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "component_not_publishable" and "category" in " ".join(response.json()["errors"]["component"])
        profile.refresh_from_db()
        assert profile.status == "DRAFT" and not OutboxEvent.objects.filter(event_type="catalog.profile_published").exists()

    def test_stale_version_on_publish(self, editor):
        profile = PublicProfileFactory(version=2)
        assert editor.post(detail(profile, "publish/"), {"expected_version": 1}, format="json").json()["code"] == "stale_version"

    def test_editing_a_published_profile_notifies_the_website(self, editor):
        profile = published(panel())
        editor.patch(detail(profile), {"summary": "Updated"}, format="json")
        assert OutboxEvent.objects.filter(event_type="catalog.profile_updated").count() == 1
        editor.delete(detail(profile))
        assert OutboxEvent.objects.filter(event_type="catalog.profile_unpublished").count() == 1


def test_list_filters_and_query_budget(editor, django_assert_max_num_queries):
    for _ in range(12):
        published(panel(primary_image=MediaAssetFactory()))
    PublicProfileFactory()
    assert editor.get(URL, {"status": "DRAFT"}).json()["count"] == 1
    with django_assert_max_num_queries(10):
        assert editor.get(URL, {"page_size": 50}).json()["count"] == 13
