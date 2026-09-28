"""content/authors/, content/categories/, content/tags/, content/badges/."""

import pytest

from audit.models import AuditLog
from blog.models import Author, Category, Tag
from blog.tests.factories import AuthorFactory, BadgeFactory, CategoryFactory, EntryFactory, PublishedEntryFactory, TagFactory
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
BASE = "/api/v1/content/"


@pytest.fixture
def editor(make_user):
    return make_user(grants={"blogs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


@pytest.mark.parametrize(("path", "payload"), [("authors", {"name": "Anu Thomas"}), ("categories", {"name": "Solar Basics"}), ("tags", {"name": "Net metering"}), ("badges", {"name": "Reviewed"})])
class TestCrud:
    def test_anonymous_and_forbidden(self, api_client, auth_client, make_user, path, payload):
        assert api_client.get(f"{BASE}{path}/").status_code == 401
        viewer = auth_client(make_user(grants={"blogs": ["view"]}))
        assert viewer.get(f"{BASE}{path}/").status_code == 200
        assert viewer.post(f"{BASE}{path}/", payload, format="json").status_code == 403

    def test_create_generates_slug_and_public_number(self, client, editor, path, payload):
        first = client.post(f"{BASE}{path}/", payload, format="json")
        assert first.status_code == 201, first.json()
        assert first.json()["slug"] and first.json()["entry_count"] == 0
        noun = {"authors": "author", "categories": "category", "tags": "tag", "badges": "badge"}[path]
        assert AuditLog.objects.get(action=f"blog.{noun}_created").actor == editor

    def test_update_delete_and_stale_version(self, client, path, payload):
        created = client.post(f"{BASE}{path}/", payload, format="json").json()
        url = f"{BASE}{path}/{created['uid']}/"
        assert client.patch(url, {"name": "Renamed", "expected_version": 1}, format="json").json()["name"] == "Renamed"
        stale = client.patch(url, {"name": "Late", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert client.delete(url).status_code == 204 and client.get(url).status_code == 404

    def test_blank_name_is_refused(self, client, path, payload):
        response = client.post(f"{BASE}{path}/", {**payload, "name": "   "}, format="json")
        assert response.status_code == 400


def test_generated_slugs_are_unique(client):
    first = client.post(f"{BASE}categories/", {"name": "Solar Basics"}, format="json").json()
    second = client.post(f"{BASE}categories/", {"name": "Solar basics!"}, format="json").json()
    assert (first["slug"], second["slug"]) == ("solar-basics", "solar-basics-2")
    assert client.post(f"{BASE}categories/", {"name": "x", "slug": "solar-basics"}, format="json").json()["code"] == "category_taken"
    assert client.post(f"{BASE}categories/", {"name": "x", "slug": "Bad Slug"}, format="json").json()["code"] == "slug_malformed"


def test_public_numbers_continue_one_counter():
    from blog.services import taxonomy

    first = taxonomy.create(taxonomy.TAG, user=None, data={"name": "One"})
    second = taxonomy.create(taxonomy.TAG, user=None, data={"name": "Two"})
    assert second.delivery_id == first.delivery_id + 1


def test_tag_names_are_unique(client):
    TagFactory(name="Kerala")
    assert client.post(f"{BASE}tags/", {"name": "Kerala"}, format="json").json()["code"] == "tag_taken"


def test_badge_colour_is_validated(client):
    assert client.post(f"{BASE}badges/", {"name": "Hot", "color": "red"}, format="json").json()["code"] == "color_malformed"
    assert client.post(f"{BASE}badges/", {"name": "Hot", "color": "#ff0000"}, format="json").json()["color"] == "#ff0000"


def test_author_avatar_must_be_a_public_image_and_user_link(client, editor):
    private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
    response = client.post(f"{BASE}authors/", {"name": "Anu", "avatar_uid": str(private.uid)}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "invalid_media"
    avatar = MediaAssetFactory()
    body = client.post(f"{BASE}authors/", {"name": "Anu", "avatar_uid": str(avatar.uid), "user_uid": str(editor.uid), "role": "Engineer"}, format="json").json()
    assert body["avatar"]["uid"] == str(avatar.uid) and body["user"] == str(editor.uid)
    assert Author.objects.get(uid=body["uid"]).user == editor


@pytest.mark.parametrize(
    ("path", "factory", "link"), [("authors", AuthorFactory, "author"), ("categories", CategoryFactory, "categories"), ("tags", TagFactory, "tags"), ("badges", BadgeFactory, "badges")]
)
def test_terms_in_use_cannot_be_deleted(client, path, factory, link):
    term = factory()
    entry = EntryFactory(author=term) if link == "author" else EntryFactory()
    if link != "author":
        getattr(entry, link).add(term)
    response = client.delete(f"{BASE}{path}/{term.uid}/")
    assert response.status_code == 409 and response.json()["code"].endswith("_in_use")
    assert client.get(f"{BASE}{path}/{term.uid}/").json()["entry_count"] == 1


def test_renaming_a_shown_term_revalidates_its_published_entries(client):
    category = CategoryFactory()
    published, draft = PublishedEntryFactory(), EntryFactory()
    published.categories.add(category)
    draft.categories.add(category)
    client.patch(f"{BASE}categories/{category.uid}/", {"name": "Renamed"}, format="json")
    event = OutboxEvent.objects.get(event_type="blog.content_changed")
    assert event.payload["paths"] == [published.collection.path_prefix, f"{published.collection.path_prefix}/{published.slug}"]


def test_renaming_an_unused_term_emits_nothing(client):
    tag = TagFactory()
    client.patch(f"{BASE}tags/{tag.uid}/", {"name": "Renamed"}, format="json")
    assert not OutboxEvent.objects.filter(event_type="blog.content_changed").exists()


def test_search_and_ordering(client):
    CategoryFactory(name="Batteries", slug="batteries")
    CategoryFactory(name="Solar Basics", slug="solar-basics")
    assert [c["name"] for c in client.get(f"{BASE}categories/?search=solar").json()["results"]] == ["Solar Basics"]
    assert [c["name"] for c in client.get(f"{BASE}categories/?ordering=-name").json()["results"]] == ["Solar Basics", "Batteries"]


def test_a_legacy_category_without_a_slug_is_served_and_editable(client):
    legacy = CategoryFactory(name="Uncategorised", slug=None)  # imported from the CMS as-is (delivered "slug": null)
    entry = EntryFactory()
    entry.categories.add(legacy)
    url = f"{BASE}categories/{legacy.uid}/"
    assert client.get(url).json()["slug"] is None
    assert client.get(f"/api/v1/content/entries/{entry.uid}/").json()["categories"] == [{"uid": str(legacy.uid), "name": "Uncategorised", "slug": None}]
    renamed = client.patch(url, {"name": "General"}, format="json").json()
    assert renamed["name"] == "General" and renamed["slug"] is None  # a rename alone never invents a public slug
    assert client.patch(url, {"slug": ""}, format="json").json()["slug"] == "general"  # an explicit blank generates one


def test_term_list_has_no_n_plus_one(client, django_assert_max_num_queries):
    for category in CategoryFactory.create_batch(2):
        EntryFactory().categories.add(category)
    with django_assert_max_num_queries(12) as small:
        client.get(f"{BASE}categories/")
    for category in CategoryFactory.create_batch(10):
        EntryFactory().categories.add(category)
    with django_assert_max_num_queries(len(small.captured_queries)):
        assert client.get(f"{BASE}categories/").json()["count"] == 12
    assert Category.objects.count() == 12 and Tag.objects.count() == 0
