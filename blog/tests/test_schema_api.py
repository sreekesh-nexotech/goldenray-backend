"""content/collections/, content/templates/ (+ duplicate/) and the nested image-groups/ and attribute-slots/."""

import pytest

from audit.models import AuditLog
from blog.models import Template, TemplateAttributeSlot, TemplateImageGroup
from blog.tests.factories import AttributeSlotFactory, CollectionFactory, EntryFactory, ImageGroupFactory, PublishedEntryFactory, TemplateFactory
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db
COLLECTIONS = "/api/v1/content/collections/"
TEMPLATES = "/api/v1/content/templates/"
NEW_COLLECTION = {"api_uid": "case-studies", "singular_name": "Case study", "plural_name": "Case studies", "path_prefix": "/case-studies"}


@pytest.fixture
def editor(make_user):
    return make_user(grants={"blogs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


class TestPermissions:
    @pytest.mark.parametrize("url", [COLLECTIONS, TEMPLATES])
    def test_anonymous_is_401(self, api_client, url):
        assert api_client.get(url).status_code == 401
        assert api_client.post(url, {}, format="json").status_code == 401

    def test_each_action_needs_its_grant(self, auth_client, make_user):
        collection = CollectionFactory()
        viewer = auth_client(make_user(grants={"blogs": ["view"]}))
        assert viewer.get(COLLECTIONS).status_code == 200
        assert viewer.post(COLLECTIONS, NEW_COLLECTION, format="json").status_code == 403
        assert viewer.patch(f"{COLLECTIONS}{collection.uid}/", {"plural_name": "x"}, format="json").status_code == 403
        assert viewer.delete(f"{COLLECTIONS}{collection.uid}/").status_code == 403
        creator = auth_client(make_user(grants={"blogs": ["view", "create"]}))
        assert creator.post(COLLECTIONS, NEW_COLLECTION, format="json").status_code == 201
        assert creator.delete(f"{COLLECTIONS}{collection.uid}/").status_code == 403
        outsider = auth_client(make_user(grants={"faqs": "*"}))
        assert outsider.get(COLLECTIONS).status_code == 403 and outsider.get(TEMPLATES).status_code == 403

    def test_scope_is_all_for_blogs(self, auth_client, make_user):
        CollectionFactory.create_batch(2)
        client = auth_client(make_user(grants={"blogs": ["view"]}, scopes={"blogs": "all"}))
        assert client.get(COLLECTIONS).json()["count"] == 2


class TestCollections:
    def test_create_list_update_delete(self, client, editor):
        response = client.post(COLLECTIONS, NEW_COLLECTION, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["api_uid"] == "case-studies" and body["entry_count"] == 0 and body["version"] == 1
        assert AuditLog.objects.get(action="blog.collection_created").actor == editor
        url = f"{COLLECTIONS}{body['uid']}/"
        updated = client.patch(url, {"plural_name": "Stories", "expected_version": 1}, format="json")
        assert updated.status_code == 200 and updated.json()["plural_name"] == "Stories" and updated.json()["version"] == 2
        assert client.patch(url, {"plural_name": "Late", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert client.delete(url).status_code == 204
        assert client.get(url).status_code == 404

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [("api_uid", "Case Studies", "api_uid_malformed"), ("api_uid", "preview", "api_uid_reserved"), ("path_prefix", "blog/", "path_prefix_malformed")],
    )
    def test_validation(self, client, field, value, code):
        response = client.post(COLLECTIONS, {**NEW_COLLECTION, field: value}, format="json")
        assert response.status_code == 400 and response.json()["code"] == code and field in response.json()["errors"]

    def test_missing_fields_use_the_error_envelope(self, client):
        body = client.post(COLLECTIONS, {}, format="json").json()
        assert body["code"] == "validation_error" and {"api_uid", "plural_name", "path_prefix"} <= set(body["errors"])

    def test_route_is_unique_among_live_collections(self, client):
        CollectionFactory(api_uid="case-studies")
        response = client.post(COLLECTIONS, NEW_COLLECTION, format="json")
        assert response.status_code == 409 and response.json()["code"] == "api_uid_taken"

    def test_in_use_collection_cannot_be_deleted(self, client):
        entry = EntryFactory()
        response = client.delete(f"{COLLECTIONS}{entry.collection.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "collection_in_use"

    def test_route_change_revalidates_published_entries(self, client):
        entry = PublishedEntryFactory()
        client.patch(f"{COLLECTIONS}{entry.collection.uid}/", {"path_prefix": "/articles"}, format="json")
        event = OutboxEvent.objects.get(event_type="blog.content_changed")
        assert "/articles" in event.payload["paths"] and f"/articles/{entry.slug}" in event.payload["paths"]

    def test_list_has_no_n_plus_one(self, client, django_assert_max_num_queries):
        for collection in CollectionFactory.create_batch(3):
            EntryFactory(collection=collection)
        with django_assert_max_num_queries(12) as small:
            client.get(COLLECTIONS)
        for collection in CollectionFactory.create_batch(12):
            EntryFactory(collection=collection)
        with django_assert_max_num_queries(len(small.captured_queries)):
            assert client.get(COLLECTIONS).json()["count"] == 15


class TestTemplates:
    def test_create_with_groups_and_slots(self, client):
        template = client.post(TEMPLATES, {"slug": "solar-guide", "name": "Solar guide"}, format="json").json()
        base = f"{TEMPLATES}{template['uid']}"
        group = client.post(f"{base}/image-groups/", {"key": "bodyImages", "label": "Body", "repeatable": True, "max_items": 8}, format="json")
        assert group.status_code == 201, group.json()
        slot = client.post(f"{base}/attribute-slots/", {"key": "difficulty", "label": "Difficulty", "type": "ENUM", "options": {"choices": ["Beginner", "Advanced"]}}, format="json")
        assert slot.status_code == 201, slot.json()
        detail = client.get(f"{base}/").json()
        assert [g["key"] for g in detail["image_groups"]] == ["bodyImages"] and detail["attribute_slots"][0]["options"] == {"choices": ["Beginner", "Advanced"]}
        assert client.get(f"{base}/image-groups/").json()["count"] == 1
        assert client.get(f"{base}/attribute-slots/{slot.json()['uid']}/").json()["key"] == "difficulty"

    def test_slug_rules(self, client):
        TemplateFactory(slug="solar-guide")
        assert client.post(TEMPLATES, {"slug": "Solar Guide", "name": "x"}, format="json").json()["code"] == "slug_malformed"
        assert client.post(TEMPLATES, {"slug": "solar-guide", "name": "x"}, format="json").json()["code"] == "slug_taken"

    def test_update_and_stale(self, client):
        template = TemplateFactory()
        url = f"{TEMPLATES}{template.uid}/"
        assert client.patch(url, {"name": "Renamed", "expected_version": 1}, format="json").json()["name"] == "Renamed"
        assert client.patch(url, {"name": "Again", "expected_version": 1}, format="json").status_code == 409

    def test_in_use_template_cannot_be_deleted(self, client):
        template = TemplateFactory()
        EntryFactory(template=template)
        assert client.delete(f"{TEMPLATES}{template.uid}/").json()["code"] == "template_in_use"
        unused = TemplateFactory()
        assert client.delete(f"{TEMPLATES}{unused.uid}/").status_code == 204

    def test_duplicate_copies_groups_and_slots(self, client):
        source = TemplateFactory(name="Guide")
        ImageGroupFactory(template=source, key="coverImg")
        AttributeSlotFactory(template=source, key="readTime", type="NUMBER")
        response = client.post(f"{TEMPLATES}{source.uid}/duplicate/", {"slug": "guide-copy"}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["name"] == "Guide (copy)" and [g["key"] for g in body["image_groups"]] == ["coverImg"] and body["attribute_slots"][0]["type"] == "NUMBER"
        assert client.post(f"{TEMPLATES}{source.uid}/duplicate/", {"slug": "guide-copy"}, format="json").json()["code"] == "slug_taken"
        assert Template.objects.filter(slug="guide-copy").count() == 1  # the failed copy left nothing behind

    def test_template_list_has_no_n_plus_one(self, client, django_assert_max_num_queries):
        for template in TemplateFactory.create_batch(2):
            ImageGroupFactory(template=template)
            AttributeSlotFactory(template=template)
        with django_assert_max_num_queries(14) as small:
            client.get(TEMPLATES)
        for template in TemplateFactory.create_batch(8):
            ImageGroupFactory(template=template)
            AttributeSlotFactory(template=template)
        with django_assert_max_num_queries(len(small.captured_queries)):
            assert client.get(TEMPLATES).json()["count"] == 10


class TestGroupsAndSlots:
    def test_keys_are_immutable_and_unique(self, client):
        group = ImageGroupFactory(key="coverImg")
        base = f"{TEMPLATES}{group.template.uid}/image-groups/"
        assert client.post(base, {"key": "coverImg", "label": "x"}, format="json").json()["code"] == "key_taken"
        assert client.post(base, {"key": "cover-img", "label": "x"}, format="json").json()["code"] == "key_malformed"
        response = client.patch(f"{base}{group.uid}/", {"key": "other"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "key_immutable"
        assert client.patch(f"{base}{group.uid}/", {"label": "Cover"}, format="json").json()["label"] == "Cover"

    def test_max_items_needs_a_repeatable_group(self, client):
        template = TemplateFactory()
        base = f"{TEMPLATES}{template.uid}/image-groups/"
        assert client.post(base, {"key": "coverImg", "label": "x", "max_items": 2}, format="json").json()["errors"] == {"max_items": ["Only a repeatable group can have max_items."]}
        group = client.post(base, {"key": "gallery", "label": "x", "repeatable": True, "max_items": 2}, format="json").json()
        assert client.patch(f"{base}{group['uid']}/", {"repeatable": False}, format="json").status_code == 400

    def test_repeatable_change_revalidates_published_entries(self, client):
        group = ImageGroupFactory(key="gallery")
        PublishedEntryFactory(template=group.template)
        client.patch(f"{TEMPLATES}{group.template.uid}/image-groups/{group.uid}/", {"repeatable": True}, format="json")
        assert OutboxEvent.objects.filter(event_type="blog.content_changed").exists()

    def test_delete_group_and_slot(self, client):
        group, slot = ImageGroupFactory(), AttributeSlotFactory()
        assert client.delete(f"{TEMPLATES}{group.template.uid}/image-groups/{group.uid}/").status_code == 204
        assert client.delete(f"{TEMPLATES}{slot.template.uid}/attribute-slots/{slot.uid}/").status_code == 204
        assert not TemplateImageGroup.objects.exists() and not TemplateAttributeSlot.objects.exists()

    @pytest.mark.parametrize(
        ("slot_type", "options"),
        [
            ("ENUM", {}),
            ("ENUM", {"choices": []}),
            ("ENUM", {"choices": ["a", "a"]}),
            ("NUMBER", {"min": 5, "max": 1}),
            ("NUMBER", {"step": 1}),
            ("NUMBER", {"min": 1, "default": 0}),
            ("TEXT", {"choices": ["a"]}),
            ("TEXT", [1]),
        ],
    )
    def test_slot_options_are_validated(self, client, slot_type, options):
        template = TemplateFactory()
        response = client.post(f"{TEMPLATES}{template.uid}/attribute-slots/", {"key": "k", "label": "K", "type": slot_type, "options": options}, format="json")
        assert response.status_code == 400 and response.json()["code"] in ("invalid_options", "validation_error")

    def test_slot_update_revalidates_options(self, client):
        slot = AttributeSlotFactory(type="ENUM", options={"choices": ["a"]})
        url = f"{TEMPLATES}{slot.template.uid}/attribute-slots/{slot.uid}/"
        assert client.patch(url, {"type": "NUMBER"}, format="json").json()["code"] == "invalid_options"
        response = client.patch(url, {"type": "NUMBER", "options": {"min": 0}}, format="json")
        assert response.status_code == 200 and response.json()["options"] == {"min": 0}
        assert client.patch(url, {"key": "other"}, format="json").json()["code"] == "key_immutable"

    def test_unknown_or_deleted_parent_is_404(self, client):
        template = TemplateFactory()
        template.soft_delete()
        assert client.get(f"{TEMPLATES}{template.uid}/image-groups/").status_code == 404
