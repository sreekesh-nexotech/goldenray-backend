"""content/entries/ CRUD, the one-call write with replace-all children, slug rules and check-slug/."""

import pytest

from audit.models import AuditLog
from blog.models import ContentBlock, Entry, EntryAttributeValue, EntryImage, EntrySeo, EntrySlugHistory
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
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/content/entries/"
BLOCKS = [{"type": "paragraph", "children": [{"type": "text", "text": "Hello"}]}]


@pytest.fixture
def editor(make_user):
    return make_user(grants={"blogs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


@pytest.fixture
def template():
    template = TemplateFactory(slug="solar-guide")
    ImageGroupFactory(template=template, key="coverImg")
    ImageGroupFactory(template=template, key="bodyImages", repeatable=True)
    AttributeSlotFactory(template=template, key="difficulty", type="ENUM", options={"choices": ["Beginner", "Advanced"]})
    AttributeSlotFactory(template=template, key="readTime", type="NUMBER", options={"min": 0})
    return template


def payload(collection, **extra):
    return {"collection_uid": str(collection.uid), "title": "How net metering works", "slug": "how-net-metering-works", **extra}


def detail(entry, suffix=""):
    return f"{URL}{entry.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        entry = EntryFactory()
        assert api_client.get(URL).status_code == 401 and api_client.get(detail(entry)).status_code == 401

    def test_actions_need_their_grants(self, auth_client, make_user):
        entry, collection = EntryFactory(), CollectionFactory()
        viewer = auth_client(make_user(grants={"blogs": ["view"]}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(entry)).status_code == 200
        assert viewer.post(URL, payload(collection), format="json").status_code == 403
        assert viewer.patch(detail(entry), {"title": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(entry)).status_code == 403
        editor = auth_client(make_user(grants={"blogs": ["view", "edit"]}))
        assert editor.patch(detail(entry), {"title": "x"}, format="json").status_code == 200
        assert editor.delete(detail(entry)).status_code == 403
        assert auth_client(make_user(grants={"pages": "*"})).get(URL).status_code == 403


class TestCreate:
    def test_one_call_saves_everything(self, client, editor, template):
        collection, author = CollectionFactory(), AuthorFactory()
        category, tag, badge = CategoryFactory(), TagFactory(), BadgeFactory()
        cover = MediaAssetFactory()
        body = payload(
            collection,
            template_uid=str(template.uid),
            excerpt="Short.",
            summary=BLOCKS,
            introduction=BLOCKS,
            warning="Check the rules.",
            read_time=5,
            is_featured=True,
            author_uid=str(author.uid),
            cover_image_uid=str(cover.uid),
            category_uids=[str(category.uid)],
            tag_uids=[str(tag.uid)],
            badge_uids=[str(badge.uid)],
            content_blocks=[{"data": BLOCKS}, {"kind": "QUOTE", "data": {"text": "Sun"}, "position": 5}],
            images=[{"group_key": "coverImg", "media_asset_uid": str(cover.uid)}, {"group_key": "bodyImages", "external_url": "https://cdn.example.com/a.png", "position": 1}],
            attribute_values=[{"slot_key": "difficulty", "value": "Beginner"}, {"slot_key": "readTime", "value": 6}],
            seo={"seo_title": "Net metering", "meta_description": "About net metering.", "keywords": "solar, kerala", "og_image_uid": str(cover.uid)},
        )
        response = client.post(URL, body, format="json")
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["status"] == "DRAFT" and data["version"] == 1 and data["path"] == f"{collection.path_prefix}/how-net-metering-works"
        assert [b["component"] for b in data["content_blocks"]] == ["shared.rich-text", "shared.quote"]
        assert [i["url"] for i in data["images"]] == ["https://cdn.example.com/a.png", cover.cdn_url]
        assert {a["slot_key"]: a["value"] for a in data["attribute_values"]} == {"difficulty": "Beginner", "readTime": 6}
        assert data["seo"]["keywords"] == "solar, kerala" and data["seo"]["og_image"]["uid"] == str(cover.uid) and data["seo"]["seo_status"] == "warning"
        assert data["categories"][0]["uid"] == str(category.uid) and data["author"]["name"] == author.name
        entry = Entry.objects.get(uid=data["uid"])
        assert entry.created_by == editor and entry.delivery_id > 0
        assert ContentBlock.objects.filter(entry=entry).count() == 2 and all(block.delivery_id for block in entry.content_blocks.all())
        assert AuditLog.objects.get(action="blog.entry_created").after["children"] == ["categories", "tags", "badges", "content_blocks", "images", "attribute_values", "seo"]

    @pytest.mark.parametrize(
        ("extra", "field", "code"),
        [
            ({"slug": "test-2"}, "slug", "slug_placeholder"),
            ({"slug": "Not A Slug"}, "slug", "slug_malformed"),
            ({"slug": "admin"}, "slug", "slug_reserved"),
            ({"images": [{"group_key": "coverImg"}]}, "images", "validation_error"),
            ({"images": [{"group_key": "cover-img", "external_url": "https://x.example.com/a.png"}]}, "images", "validation_error"),
            ({"attribute_values": [{"slot_key": "difficulty", "value": "x"}]}, "attribute_values", "template_required"),
            ({"content_blocks": [{"kind": "RICH_TEXT", "data": {"not": "a list"}}]}, "content_blocks", "validation_error"),
            ({"content_blocks": [{"component": "Bad Component", "data": []}]}, "content_blocks", "validation_error"),
            ({"read_time": -1}, "read_time", "validation_error"),
            ({"locale": "eng"}, "locale", "validation_error"),
            ({"seo": {"schema_extra": ["not", "an", "object"]}}, "seo.schema_extra", "validation_error"),
        ],
    )
    def test_validation_envelope(self, client, extra, field, code):
        response = client.post(URL, payload(CollectionFactory(), **extra), format="json")
        assert response.status_code == 400, response.json()
        assert response.json()["code"] == code and field in response.json()["errors"]

    def test_values_are_validated_against_the_template(self, client, template):
        base = payload(CollectionFactory(), template_uid=str(template.uid))
        cases = [
            ({"images": [{"group_key": "unknown", "external_url": "https://x.example.com/a.png"}]}, "images"),
            ({"attribute_values": [{"slot_key": "difficulty", "value": "Expert"}]}, "attribute_values"),
            ({"attribute_values": [{"slot_key": "readTime", "value": "six"}]}, "attribute_values"),
            ({"attribute_values": [{"slot_key": "readTime", "value": -3}]}, "attribute_values"),
            ({"attribute_values": [{"slot_key": "nope", "value": 1}]}, "attribute_values"),
            ({"attribute_values": [{"slot_key": "difficulty", "value": "Beginner"}, {"slot_key": "difficulty", "value": "Beginner"}]}, "attribute_values"),
        ]
        for extra, field in cases:
            response = client.post(URL, {**base, **extra}, format="json")
            assert response.status_code == 400 and field in response.json()["errors"], (extra, response.json())

    @pytest.mark.parametrize("asset_kwargs", [{"visibility": "PRIVATE", "cdn_url": ""}, {"kind": "DOCUMENT"}])
    def test_media_must_be_public_images(self, client, template, asset_kwargs):
        asset = MediaAssetFactory(**asset_kwargs)
        cover = client.post(URL, payload(CollectionFactory(), cover_image_uid=str(asset.uid)), format="json")
        assert cover.status_code == 400 and cover.json()["code"] == "invalid_media"
        image = client.post(URL, payload(CollectionFactory(), template_uid=str(template.uid), images=[{"group_key": "coverImg", "media_asset_uid": str(asset.uid)}]), format="json")
        assert image.status_code == 400 and "images" in image.json()["errors"]
        seo = client.post(URL, payload(CollectionFactory(), seo={"og_image_uid": str(asset.uid)}), format="json")
        assert seo.status_code == 400 and seo.json()["code"] == "invalid_media"

    def test_slug_is_unique_per_collection_including_aliases(self, client):
        entry = EntryFactory(slug="how-net-metering-works")
        taken = client.post(URL, payload(entry.collection), format="json")
        assert taken.status_code == 409 and taken.json()["code"] == "slug_taken"
        assert client.post(URL, payload(CollectionFactory()), format="json").status_code == 201  # another collection is fine
        alias = AliasFactory(slug="net-metering-explained")
        response = client.post(URL, payload(alias.collection, slug="net-metering-explained"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "slug_taken"

    def test_a_deleted_entry_frees_its_slug(self, client):
        entry = EntryFactory(slug="how-net-metering-works")
        entry.soft_delete()
        assert client.post(URL, payload(entry.collection), format="json").status_code == 201


class TestUpdate:
    def test_partial_update_and_stale_version(self, client):
        entry = EntryFactory()
        response = client.patch(detail(entry), {"title": "New title", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["title"] == "New title" and response.json()["version"] == 2
        stale = client.patch(detail(entry), {"title": "Older", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_children_are_replace_all_and_kept_when_omitted(self, client, template):
        entry = EntryFactory(template=template)
        ContentBlockFactory(entry=entry)
        EntryImageFactory(entry=entry)
        AttributeValueFactory(entry=entry)
        tag = TagFactory()
        entry.tags.add(tag)
        client.patch(detail(entry), {"title": "Only the title"}, format="json")
        assert entry.content_blocks.count() == 1 and entry.images.count() == 1 and entry.tags.count() == 1
        response = client.patch(
            detail(entry), {"content_blocks": [{"data": BLOCKS}, {"data": BLOCKS}], "images": [], "tag_uids": [], "attribute_values": [{"slot_key": "readTime", "value": 4}]}, format="json"
        )
        assert response.status_code == 200, response.json()
        assert entry.content_blocks.count() == 2 and entry.images.count() == 0 and entry.tags.count() == 0
        assert ContentBlock.all_objects.filter(entry=entry, deleted_at__isnull=False).count() == 1  # replaced rows are retired, not destroyed
        assert list(EntryAttributeValue.objects.filter(entry=entry).values_list("slot_key", "value")) == [("readTime", 4)]
        assert "content_blocks" in AuditLog.objects.filter(action="blog.entry_updated").last().after["changed"]

    def test_seo_upsert_and_removal(self, client):
        entry = EntryFactory()
        client.patch(detail(entry), {"seo": {"seo_title": "T", "meta_description": "D"}}, format="json")
        assert EntrySeo.objects.get(entry=entry).seo_title == "T"
        client.patch(detail(entry), {"seo": {"meta_description": "Only D"}}, format="json")
        seo = EntrySeo.objects.get(entry=entry)
        assert (seo.seo_title, seo.meta_description) == ("T", "Only D")
        assert client.patch(detail(entry), {"seo": None}, format="json").json()["seo"] is None
        assert client.patch(detail(entry), {"seo": {"seo_title": "Back"}}, format="json").json()["seo"]["seo_title"] == "Back"
        assert EntrySeo.all_objects.filter(entry=entry).count() == 1

    def test_rename_keeps_the_old_slug_as_alias(self, client):
        entry = PublishedEntryFactory(slug="old-name")
        response = client.patch(detail(entry), {"slug": "new-name"}, format="json")
        assert response.status_code == 200
        assert [(a["slug"], a["active"]) for a in response.json()["slug_history"]] == [("old-name", True)]
        event = OutboxEvent.objects.get(event_type="blog.entry_slug_changed")
        prefix = entry.collection.path_prefix
        assert event.payload["paths"] == [prefix, f"{prefix}/new-name", f"{prefix}/old-name"] and event.payload["old_slug"] == "old-name"
        # Taking the old slug back retires the alias instead of fighting it.
        client.patch(detail(entry), {"slug": "old-name"}, format="json")
        assert dict(EntrySlugHistory.objects.filter(entry=entry).values_list("slug", "active")) == {"old-name": False, "new-name": True}

    def test_rename_to_a_taken_slug_is_refused(self, client):
        entry = EntryFactory()
        other = EntryFactory(collection=entry.collection)
        assert client.patch(detail(entry), {"slug": other.slug}, format="json").json()["code"] == "slug_taken"

    def test_published_edit_emits_an_update_event_and_draft_edit_does_not(self, client):
        draft, published = EntryFactory(), PublishedEntryFactory()
        client.patch(detail(draft), {"title": "Draft change"}, format="json")
        assert not OutboxEvent.objects.exists()
        client.patch(detail(published), {"title": "Live change"}, format="json")
        assert OutboxEvent.objects.get().event_type == "blog.entry_updated"

    def test_collection_is_immutable(self, client):
        entry = EntryFactory()
        response = client.patch(detail(entry), {"collection_uid": str(CollectionFactory().uid)}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "collection_immutable"
        assert client.patch(detail(entry), {"collection_uid": str(entry.collection.uid)}, format="json").status_code == 200

    def test_template_change_must_fit_the_rows_kept(self, client, template):
        entry = EntryFactory(template=template)
        EntryImageFactory(entry=entry, group_key="bodyImages")
        AttributeValueFactory(entry=entry, slot_key="difficulty")
        other = TemplateFactory()
        response = client.patch(detail(entry), {"template_uid": str(other.uid)}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "template_mismatch"
        ok = client.patch(detail(entry), {"template_uid": str(other.uid), "images": [], "attribute_values": []}, format="json")
        assert ok.status_code == 200, ok.json()
        attrs = EntryFactory(template=template)
        AttributeValueFactory(entry=attrs, slot_key="difficulty")
        assert client.patch(detail(attrs), {"template_uid": None}, format="json").json()["code"] == "template_mismatch"

    def test_no_change_writes_nothing(self, client):
        entry = EntryFactory(title="Same")
        assert client.patch(detail(entry), {"title": "Same"}, format="json").json()["version"] == 1


class TestDelete:
    def test_soft_delete_retires_aliases_and_revalidates_if_public(self, client):
        alias = AliasFactory()
        entry = alias.entry
        assert client.delete(detail(entry)).status_code == 204
        assert Entry.all_objects.get(pk=entry.pk).deleted_at is not None and not EntrySlugHistory.objects.get().active
        assert OutboxEvent.objects.get().event_type == "blog.entry_deleted"
        assert client.get(detail(entry)).status_code == 404

    def test_stale_delete(self, client):
        entry = EntryFactory()
        assert client.delete(f"{detail(entry)}?expected_version=9").status_code == 409


class TestList:
    def test_filters_and_default_archived_exclusion(self, client, template):
        collection = CollectionFactory(api_uid="articles")
        category = CategoryFactory()
        live = PublishedEntryFactory(collection=collection, template=template, is_featured=True)
        live.categories.add(category)
        draft = EntryFactory(collection=collection)
        archived = EntryFactory(status=Entry.Status.ARCHIVED, archived_at=live.published_at)
        EntryFactory()  # another collection

        def uids(query=""):
            return {row["uid"] for row in client.get(f"{URL}?{query}").json()["results"]}

        assert str(archived.uid) not in uids()
        assert uids("status=ARCHIVED") == {str(archived.uid)} and str(archived.uid) in uids("include_archived=true")
        assert uids("collection=articles") == {str(live.uid), str(draft.uid)} == uids(f"collection={collection.uid}")
        assert uids(f"category={category.uid}") == {str(live.uid)} and uids(f"template={template.uid}") == {str(live.uid)}
        assert uids("status=PUBLISHED&is_featured=true") == {str(live.uid)} and uids("collection=nope") == set()
        assert uids(f"search={draft.slug}") == {str(draft.uid)}

    def test_list_rows_are_slim_and_have_a_cover_url(self, client):
        entry = EntryFactory()
        EntryImageFactory(entry=entry, group_key="bodyImages", external_url="https://cdn.example.com/b.png")
        row = client.get(URL).json()["results"][0]
        assert row["cover_url"] == "https://cdn.example.com/b.png" and "content_blocks" not in row and row["collection"]["api_uid"] == entry.collection.api_uid

    def test_list_has_no_n_plus_one(self, client, django_assert_max_num_queries):
        def seed(count):
            for _ in range(count):
                entry = EntryFactory(author=AuthorFactory(), cover_image=MediaAssetFactory())
                EntryImageFactory(entry=entry)

        seed(2)
        with django_assert_max_num_queries(12) as small:
            client.get(URL)
        seed(10)
        with django_assert_max_num_queries(len(small.captured_queries)):
            assert client.get(URL).json()["count"] == 12

    def test_detail_has_a_bounded_query_count(self, client, django_assert_max_num_queries, template):
        entry = PublishedEntryFactory(template=template, author=AuthorFactory())
        for _ in range(5):
            ContentBlockFactory(entry=entry)
            EntryImageFactory(entry=entry, group_key="bodyImages")
            entry.categories.add(CategoryFactory())
        EntrySeoFactory(entry=entry)
        with django_assert_max_num_queries(20):
            assert client.get(detail(entry)).status_code == 200


class TestCheckSlug:
    def test_availability_suggestion_and_validity(self, client):
        entry = EntryFactory(slug="solar-guide")
        base = f"{URL}check-slug/?collection={entry.collection.api_uid}"
        assert client.get(f"{base}&slug=Solar Guide").json() == {"slug": "solar-guide", "available": False, "suggestion": "solar-guide-2", "valid": True, "error": None}
        assert client.get(f"{base}&slug=solar-guide&exclude={entry.uid}").json()["available"] is True
        assert client.get(f"{base}&slug=test").json()["valid"] is False
        assert client.get(f"{URL}check-slug/?collection={entry.collection.uid}&slug=new-one").json()["available"] is True
        assert client.get(f"{URL}check-slug/?collection=unknown&slug=x").status_code == 404
        assert client.get(f"{URL}check-slug/?slug=x").status_code == 400

    def test_aliases_count_as_taken(self, client):
        alias = AliasFactory(slug="old-guide")
        body = client.get(f"{URL}check-slug/?collection={alias.collection.api_uid}&slug=old-guide").json()
        assert body["available"] is False and body["suggestion"] == "old-guide-2"
        assert EntryImage.objects.count() == 0
