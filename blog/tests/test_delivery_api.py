"""Public delivery beyond the captured parity cases: shape rules, fixed legacy weaknesses, cache, throttle, N+1."""

import pytest
from django.utils import timezone

from blog.models import Entry
from blog.services import legacy_import
from blog.tests.factories import (
    AliasFactory,
    AttributeSlotFactory,
    AttributeValueFactory,
    AuthorFactory,
    BadgeFactory,
    CategoryFactory,
    CollectionFactory,
    ContentBlockFactory,
    EntryFactory,
    EntryImageFactory,
    EntrySeoFactory,
    ImageGroupFactory,
    PublishedEntryFactory,
    TagFactory,
    TemplateFactory,
)
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def collection():
    return CollectionFactory(api_uid="articles", path_prefix="/blog")


def url(collection, query=""):
    return f"/api/public/v1/content/{collection.api_uid}/" + (f"?{query}" if query else "")


def data(api_client, collection, query=""):
    response = api_client.get(url(collection, query))
    assert response.status_code == 200, response.content
    return response.json()["data"]


class TestRoutes:
    def test_unknown_inactive_and_deleted_collections_are_404(self, api_client, collection):
        assert api_client.get("/api/public/v1/content/nope/").json()["code"] == "collection_not_found"
        inactive = CollectionFactory(is_active=False)
        assert api_client.get(url(inactive)).status_code == 404
        collection.soft_delete()
        assert api_client.get(url(collection)).status_code == 404

    def test_only_published_entries_are_delivered(self, api_client, collection):
        live = PublishedEntryFactory(collection=collection)
        for status in (Entry.Status.DRAFT, Entry.Status.REVIEW):
            EntryFactory(collection=collection, status=status)
        EntryFactory(collection=collection, status=Entry.Status.ARCHIVED, archived_at=timezone.now())
        deleted = PublishedEntryFactory(collection=collection)
        deleted.soft_delete()
        assert [row["documentId"] for row in data(api_client, collection)] == [str(live.uid)]

    def test_slug_route_404_and_alias(self, api_client, collection):
        entry = PublishedEntryFactory(collection=collection, slug="current-name")
        AliasFactory(entry=entry, collection=collection, slug="older-name")
        assert api_client.get(f"{url(collection)}no-such-entry/").json()["code"] == "entry_not_found"
        body = api_client.get(f"{url(collection)}older-name/").json()
        assert body["data"][0]["slug"] == "current-name" and body["meta"]["redirect"] == {"from": "older-name", "to": "current-name", "reason": "slug_changed"}

    def test_an_alias_never_resurrects_a_draft(self, api_client, collection):
        alias = AliasFactory(entry=PublishedEntryFactory(collection=collection), collection=collection, slug="older-name")
        Entry.objects.filter(pk=alias.entry.pk).update(status=Entry.Status.DRAFT)
        assert data(api_client, collection, "filters[slug][$eq]=older-name") == []

    def test_write_methods_are_not_allowed(self, api_client, collection):
        assert api_client.post(url(collection), {}, format="json").status_code == 405


class TestQueryLanguage:
    @pytest.fixture
    def entries(self, collection):
        return [
            PublishedEntryFactory(collection=collection, slug="alpha-guide", title="Alpha", read_time=3, is_featured=True, sort_order=1),
            PublishedEntryFactory(collection=collection, slug="beta-guide", title="Beta", read_time=7, sort_order=2),
            PublishedEntryFactory(collection=collection, slug="gamma-guide", title="Gamma", read_time=None, sort_order=None),
        ]

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "t"])
    def test_boolean_values_are_coerced(self, api_client, collection, entries, value):
        assert [row["slug"] for row in data(api_client, collection, f"filters[isFeatured][$eq]={value}")] == ["alpha-guide"]

    @pytest.mark.parametrize(
        "query",
        ["filters[isFeatured][$eq]=maybe", "filters[readTime][$gte]=abc", "filters[documentId][$eq]=nope", "filters[publishedOn][$gt]=yesterday", "filters[sortOrder][$in]=1,x"],
    )
    def test_unparseable_values_are_400_not_500(self, api_client, collection, entries, query):
        response = api_client.get(url(collection, query))
        assert response.status_code == 400 and response.json()["code"] == "invalid_filter"

    def test_in_accepts_commas_and_the_indexed_form(self, api_client, collection, entries):
        comma = data(api_client, collection, "filters[slug][$in]=alpha-guide,gamma-guide")
        indexed = data(api_client, collection, "filters[slug][$in][0]=alpha-guide&filters[slug][$in][1]=gamma-guide")
        assert [row["slug"] for row in comma] == [row["slug"] for row in indexed] == ["alpha-guide", "gamma-guide"]
        assert [row["slug"] for row in data(api_client, collection, "filters[readTime][$in]=3,7")] == ["alpha-guide", "beta-guide"]

    def test_fields_accept_commas(self, api_client, collection, entries):
        row = data(api_client, collection, "fields=slug,readTime&filters[slug][$eq]=alpha-guide")[0]
        assert list(row) == ["id", "documentId", "slug", "readTime"]

    def test_date_only_values_and_ne(self, api_client, collection, entries):
        today = timezone.localdate().isoformat()
        assert len(data(api_client, collection, f"filters[publishedAt][$gte]={today}")) == 3
        assert [row["slug"] for row in data(api_client, collection, "filters[slug][$ne]=beta-guide")] == ["alpha-guide", "gamma-guide"]


class TestPayload:
    def test_media_backed_cover_and_images(self, api_client, collection):
        template = TemplateFactory()
        ImageGroupFactory(template=template, key="bodyImages", repeatable=True)
        ImageGroupFactory(template=template, key="mainImg")
        cover = MediaAssetFactory(width=1920, height=1080, alternative_text="Roof")
        private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
        entry = PublishedEntryFactory(collection=collection, template=template, cover_image=cover)
        EntryImageFactory(entry=entry, group_key="bodyImages", external_url="https://cdn.example.com/one.png")
        EntryImageFactory(entry=entry, group_key="mainImg", media_asset=private, external_url="")
        EntryImageFactory(entry=entry, group_key="extra", external_url="https://cdn.example.com/x.png")
        EntryImageFactory(entry=entry, group_key="extra", external_url="https://cdn.example.com/y.png", position=1)
        row = data(api_client, collection)[0]
        assert row["coverImage"] == {"url": cover.cdn_url, "width": 1920, "height": 1080, "alternativeText": "Roof"}
        # repeatable from the template → array even with one image; private media never leaks; no template hint → inferred.
        assert row["imgUrls"] == {"bodyImages": ["https://cdn.example.com/one.png"], "extra": ["https://cdn.example.com/x.png", "https://cdn.example.com/y.png"]}

    def test_cover_falls_back_to_the_cover_group(self, api_client, collection, settings):
        settings.PUBLIC_MEDIA_BASE_URL = "https://flarize.com"
        entry = PublishedEntryFactory(collection=collection)
        asset = MediaAssetFactory(cdn_url="/media/public/cover.jpg", alternative_text="")
        EntryImageFactory(entry=entry, group_key="coverImg", media_asset=asset, external_url="")
        row = data(api_client, collection)[0]
        assert row["coverImage"] == {"url": "https://flarize.com/media/public/cover.jpg", "width": 64, "height": 48, "alternativeText": None}

    def test_attribute_fallbacks_and_escape_hatch(self, api_client, collection):
        slot = AttributeSlotFactory(key="readTime", type="NUMBER")
        AttributeSlotFactory(template=slot.template, key="difficulty", type="ENUM", options={"choices": ["Beginner"]})
        entry = PublishedEntryFactory(collection=collection, template=slot.template, read_time=None, warning="")
        AttributeValueFactory(entry=entry, slot_key="readTime", value=8)
        AttributeValueFactory(entry=entry, slot_key="difficulty", value="Beginner")
        AttributeValueFactory(entry=entry, slot_key="warning", value="From the slot")
        row = data(api_client, collection)[0]
        assert row["readTime"] == 8 and row["warning"] == "From the slot" and row["insights"] is None
        assert row["attributes"] == {"difficulty": "Beginner", "readTime": 8, "warning": "From the slot"}

    def test_embedded_objects_and_seo(self, api_client, collection):
        author = AuthorFactory(bio="", role="Engineer")
        entry = PublishedEntryFactory(collection=collection, author=author)
        entry.categories.add(CategoryFactory(name="B"), CategoryFactory(name="A"))
        entry.tags.add(TagFactory(name="Kerala"))
        entry.badges.add(BadgeFactory(name="Reviewed", color="#ED8723"))
        ContentBlockFactory(entry=entry, position=1, component="shared.quote", kind="QUOTE", data={"text": "x"})
        first = ContentBlockFactory(entry=entry, position=0)
        EntrySeoFactory(entry=entry, seo_title="T", meta_description="", canonical_url="", keywords="k")
        row = data(api_client, collection)[0]
        assert row["author"] == {"id": author.delivery_id, "name": author.name, "bio": None, "role": "Engineer"}
        assert [c["name"] for c in row["categories"]] == ["A", "B"] and row["tags"][0]["name"] == "Kerala"
        assert row["badges"] == [{"id": entry.badges.get().delivery_id, "label": "Reviewed", "color": "#ED8723"}]
        assert row["contentBlocks"][0] == {"__component": "shared.rich-text", "id": first.delivery_id, "body": first.data} and row["contentBlocks"][1]["body"] == {"text": "x"}
        assert row["seo"] == {"metaTitle": "T", "metaDescription": None, "canonicalUrl": None, "keywords": "k"}
        assert list(row) == [
            "id", "documentId", "title", "slug", "excerpt", "summary", "introduction", "readTime", "isFeatured", "sortOrder", "publishedOn", "updatedAt", "publishedAt",
            "author", "categories", "tags", "badges", "contentBlocks", "coverImage", "warning", "insights", "seo", "imgUrls", "attributes",
        ]  # fmt: skip

    def test_soft_deleted_seo_block_is_not_delivered(self, api_client, collection):
        seo = EntrySeoFactory(entry=PublishedEntryFactory(collection=collection))
        seo.soft_delete()
        assert data(api_client, collection)[0]["seo"] is None


class TestCache:
    def test_headers_etag_and_304(self, api_client, collection):
        PublishedEntryFactory(collection=collection)
        first = api_client.get(url(collection))
        assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
        assert api_client.get(url(collection))["X-Cache"] == "HIT"
        assert api_client.get(url(collection), HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
        assert api_client.get(url(collection, "pagination[page]=2"))["X-Cache"] == "MISS"  # the query is part of the key

    @pytest.mark.parametrize("change", ["entry", "author", "category", "tag", "badge", "template", "collection", "media"])
    def test_every_embedded_model_invalidates(self, api_client, auth_client, make_user, collection, change):
        from blog.services import schema, taxonomy
        from media.services.assets import update_asset

        cover = MediaAssetFactory()
        template = TemplateFactory()
        entry = PublishedEntryFactory(collection=collection, author=AuthorFactory(), template=template, cover_image=cover)
        category, tag, badge = CategoryFactory(), TagFactory(), BadgeFactory()
        entry.categories.add(category)
        entry.tags.add(tag)
        entry.badges.add(badge)
        api_client.get(url(collection))
        assert api_client.get(url(collection))["X-Cache"] == "HIT"
        user = make_user(grants={"blogs": "*", "media": "*"})
        changes = {
            "entry": lambda: auth_client(user).patch(f"/api/v1/content/entries/{entry.uid}/", {"title": "New"}, format="json"),
            "author": lambda: taxonomy.update(taxonomy.AUTHOR, entry.author, user=user, data={"name": "New name"}),
            "category": lambda: taxonomy.update(taxonomy.CATEGORY, category, user=user, data={"name": "New"}),
            "tag": lambda: taxonomy.update(taxonomy.TAG, tag, user=user, data={"name": "New"}),
            "badge": lambda: taxonomy.update(taxonomy.BADGE, badge, user=user, data={"color": "#000000"}),
            "template": lambda: schema.create_image_group(template, user=user, data={"key": "hero", "label": "Hero"}),
            "collection": lambda: schema.update_collection(collection, user=user, data={"plural_name": "Stories"}),
            "media": lambda: update_asset(cover, user=user, data={"alternative_text": "New alt"}),
        }
        changes[change]()
        assert api_client.get(url(collection))["X-Cache"] == "MISS"

    def test_throttle_scope_is_public_read(self, api_client, collection, settings):
        from blog.views.delivery import CollectionDeliveryView, EntryDeliveryView, EntryPreviewView

        for view in (CollectionDeliveryView(), EntryDeliveryView(), EntryPreviewView()):
            assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
        assert [api_client.get(url(collection)).status_code for _ in range(3)] == [200, 200, 429]


def test_delivery_list_has_no_n_plus_one(api_client, collection, django_assert_max_num_queries):
    template = TemplateFactory()
    ImageGroupFactory(template=template, key="bodyImages", repeatable=True)

    def seed(count):
        for _ in range(count):
            entry = PublishedEntryFactory(collection=collection, template=template, author=AuthorFactory(), cover_image=MediaAssetFactory())
            entry.categories.add(CategoryFactory())
            entry.tags.add(TagFactory())
            entry.badges.add(BadgeFactory())
            ContentBlockFactory(entry=entry)
            EntryImageFactory(entry=entry, group_key="bodyImages")
            EntrySeoFactory(entry=entry)

    seed(2)
    with django_assert_max_num_queries(16) as small:
        api_client.get(url(collection, "populate=*"))
    seed(12)
    with django_assert_max_num_queries(len(small.captured_queries)):
        assert len(api_client.get(url(collection, "populate=*&pagination[pageSize]=100")).json()["data"]) == 14


def test_sequences_continue_after_the_import():
    from blog.services import taxonomy

    legacy_import.import_authors([{"id": 40, "name": "Imported", "bio": None, "role": None}])
    assert taxonomy.create(taxonomy.AUTHOR, user=None, data={"name": "New"}).delivery_id == 41
