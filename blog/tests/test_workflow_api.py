"""content/entries/<uid>/ workflow actions, publish-time validation, scheduling (Beat) and preview links."""

import datetime as dt

import pytest
from django.core import signing
from django.utils import timezone
from freezegun import freeze_time

from audit.models import AuditLog
from blog.models import Entry, EntrySlugHistory
from blog.services import workflow
from blog.tasks import publish_due_entries
from blog.tests.factories import (
    AliasFactory,
    AttributeSlotFactory,
    AttributeValueFactory,
    ContentBlockFactory,
    EntryFactory,
    EntryImageFactory,
    EntrySeoFactory,
    ImageGroupFactory,
    PublishedEntryFactory,
    TemplateFactory,
)
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db
URL = "/api/v1/content/entries/"
Status = Entry.Status


@pytest.fixture
def editor(make_user):
    return make_user(grants={"blogs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


def act(client, entry, action, body=None):
    return client.post(f"{URL}{entry.uid}/{action}/", body or {}, format="json")


class TestPermissions:
    @pytest.mark.parametrize(
        ("action", "grant"),
        [
            ("submit", "edit"),
            ("publish", "publish"),
            ("unpublish", "publish"),
            ("schedule", "publish"),
            ("archive", "archive"),
            ("restore", "archive"),
            ("verify", "verify"),
            ("duplicate", "create"),
            ("preview", "view"),
        ],
    )
    def test_each_action_needs_its_grant(self, auth_client, make_user, api_client, action, grant):
        entry = EntryFactory()
        assert api_client.post(f"{URL}{entry.uid}/{action}/").status_code == 401
        others = [name for name in ("view", "create", "edit", "publish", "verify", "archive") if name != grant]
        assert act(auth_client(make_user(grants={"blogs": others})), entry, action).status_code == 403
        assert act(auth_client(make_user(grants={"blogs": [grant]})), entry, action).status_code not in (401, 403)


class TestTransitions:
    def test_submit_publish_unpublish_cycle(self, client, editor):
        entry = EntryFactory()
        assert act(client, entry, "submit").json()["status"] == "REVIEW"
        published = act(client, entry, "publish", {"expected_version": 2})
        assert published.status_code == 200, published.json()
        body = published.json()
        assert body["status"] == "PUBLISHED" and body["published_at"] and body["published_on"] == body["published_at"]
        event = OutboxEvent.objects.get(event_type="blog.entry_published")
        assert event.payload["paths"] == [entry.collection.path_prefix, f"{entry.collection.path_prefix}/{entry.slug}"] and event.dedup_key.endswith(":3")
        first_published_at = body["published_at"]
        assert act(client, entry, "unpublish").json()["status"] == "DRAFT"
        assert OutboxEvent.objects.filter(event_type="blog.entry_unpublished").exists()
        republished = act(client, entry, "publish").json()
        assert republished["published_at"] == first_published_at  # set once, never refreshed
        assert AuditLog.objects.filter(action="blog.entry_published", actor=editor).count() == 2

    def test_display_date_is_kept_when_set(self, client):
        entry = EntryFactory(published_on=timezone.make_aware(dt.datetime(2025, 1, 1)))
        assert act(client, entry, "publish").json()["published_on"].startswith("2025-01-01")

    @pytest.mark.parametrize(
        ("status", "action"),
        [
            (Status.PUBLISHED, "publish"),
            (Status.PUBLISHED, "submit"),
            (Status.DRAFT, "unpublish"),
            (Status.DRAFT, "restore"),
            (Status.ARCHIVED, "publish"),
            (Status.ARCHIVED, "archive"),
            (Status.ARCHIVED, "verify"),
        ],
    )
    def test_invalid_transitions_are_409(self, client, status, action):
        extra = {"published_at": timezone.now()} if status == Status.PUBLISHED else {"archived_at": timezone.now()} if status == Status.ARCHIVED else {}
        entry = EntryFactory(status=status, **extra)
        response = act(client, entry, action)
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"

    def test_stale_version_on_actions(self, client):
        entry = EntryFactory()
        assert act(client, entry, "publish", {"expected_version": 7}).json()["code"] == "stale_version"

    def test_archive_and_restore(self, client):
        entry = PublishedEntryFactory()
        archived = act(client, entry, "archive").json()
        assert archived["status"] == "ARCHIVED" and archived["archived_at"]
        assert OutboxEvent.objects.get().event_type == "blog.entry_archived"
        restored = act(client, entry, "restore").json()
        assert restored["status"] == "DRAFT" and restored["archived_at"] is None
        draft = EntryFactory()
        act(client, draft, "archive")
        assert OutboxEvent.objects.count() == 1  # archiving a draft changes nothing public

    def test_verify_stamps_the_verifier(self, client, editor):
        body = act(client, EntryFactory(), "verify").json()
        assert body["verified_by"]["uid"] == str(editor.uid) and body["verified_at"]


class TestPublishValidation:
    def test_template_requirements_are_enforced_at_publish_only(self, client):
        template = TemplateFactory()
        ImageGroupFactory(template=template, key="coverImg", required=True, label="Cover")
        ImageGroupFactory(template=template, key="hero", label="Hero")
        ImageGroupFactory(template=template, key="gallery", repeatable=True, max_items=1, label="Gallery")
        AttributeSlotFactory(template=template, key="difficulty", required=True, label="Difficulty", type="ENUM", options={"choices": ["Beginner"]})
        entry = EntryFactory(template=template)  # a draft saves loose
        EntryImageFactory(entry=entry, group_key="hero")
        EntryImageFactory(entry=entry, group_key="hero", position=1)
        EntryImageFactory(entry=entry, group_key="gallery")
        EntryImageFactory(entry=entry, group_key="gallery", position=1)
        response = act(client, entry, "publish")
        assert response.status_code == 400 and response.json()["code"] == "publish_validation_failed"
        errors = response.json()["errors"]
        assert errors["attribute_values"] == ["Attribute 'Difficulty' (difficulty) is required."]
        assert set(errors["images"]) == {
            "Image group 'Cover' (coverImg) requires at least one image.",
            "Image group 'Hero' (hero) is single but has 2 images.",
            "Image group 'Gallery' (gallery) exceeds max_items=1.",
        }

    def test_values_that_no_longer_fit_their_slot_block_publication(self, client):
        slot = AttributeSlotFactory(key="level", type="NUMBER", options={"max": 3})
        entry = EntryFactory(template=slot.template)
        AttributeValueFactory(entry=entry, slot_key="level", value=9)
        assert "attribute_values" in act(client, entry, "publish").json()["errors"]

    def test_zero_and_false_count_as_provided(self, client):
        slot = AttributeSlotFactory(key="level", type="NUMBER", required=True)
        entry = EntryFactory(template=slot.template)
        AttributeValueFactory(entry=entry, slot_key="level", value=0)
        assert act(client, entry, "publish").status_code == 200

    def test_invalid_legacy_slug_blocks_publication(self, client):
        entry = EntryFactory(slug="test")
        assert act(client, entry, "publish").json()["errors"]["slug"]


class TestSchedule:
    def test_schedule_then_beat_publishes(self, client):
        entry = EntryFactory()
        when = timezone.now() + dt.timedelta(hours=1)
        body = act(client, entry, "schedule", {"scheduled_for": when.isoformat()}).json()
        assert body["status"] == "DRAFT" and body["scheduled_for"]
        assert publish_due_entries() == {"published": 0, "failed": 0}
        with freeze_time(when + dt.timedelta(minutes=1)):
            assert publish_due_entries() == {"published": 1, "failed": 0}
        entry.refresh_from_db()
        assert entry.status == Status.PUBLISHED and entry.scheduled_for is None
        log = AuditLog.objects.get(action="blog.entry_published")
        assert log.actor is None and log.actor_kind == "SYSTEM"

    def test_cancel_and_validation(self, client):
        entry = EntryFactory()
        past = act(client, entry, "schedule", {"scheduled_for": (timezone.now() - dt.timedelta(minutes=1)).isoformat()})
        assert past.status_code == 400 and past.json()["code"] == "schedule_in_past"
        act(client, entry, "schedule", {"scheduled_for": (timezone.now() + dt.timedelta(days=1)).isoformat()})
        assert act(client, entry, "schedule", {"scheduled_for": None}).json()["scheduled_for"] is None
        broken = EntryFactory(slug="test")
        assert act(client, broken, "schedule", {"scheduled_for": (timezone.now() + dt.timedelta(days=1)).isoformat()}).json()["code"] == "publish_validation_failed"

    def test_a_due_entry_that_fails_validation_is_unscheduled_and_audited(self):
        slot = AttributeSlotFactory(key="level", required=True)
        entry = EntryFactory(template=slot.template, scheduled_for=timezone.now() - dt.timedelta(minutes=1))
        assert workflow.publish_due_entries() == {"published": 0, "failed": 1}
        entry.refresh_from_db()
        assert entry.status == Status.DRAFT and entry.scheduled_for is None
        assert AuditLog.objects.get(action="blog.entry_schedule_failed").after["code"] == "publish_validation_failed"
        assert workflow.publish_due_entries() == {"published": 0, "failed": 0}

    def test_scheduling_is_cleared_by_archive_and_publish(self, client):
        entry = EntryFactory(scheduled_for=timezone.now() + dt.timedelta(days=1))
        assert act(client, entry, "publish").json()["scheduled_for"] is None


class TestDuplicate:
    def test_duplicate_is_a_fresh_draft_with_children(self, client):
        entry = PublishedEntryFactory(slug="guide", verified_at=timezone.now())
        ContentBlockFactory(entry=entry)
        EntryImageFactory(entry=entry)
        EntrySeoFactory(entry=entry, keywords="a, b")
        response = act(client, entry, "duplicate")
        assert response.status_code == 201
        body = response.json()
        assert body["slug"] == "guide-copy" and body["status"] == "DRAFT" and body["title"].endswith("(copy)")
        assert body["published_at"] is None and body["published_on"] is None and body["archived_at"] is None and body["verified_at"] is None
        assert len(body["content_blocks"]) == 1 and len(body["images"]) == 1 and body["seo"]["keywords"] == "a, b"
        assert act(client, entry, "duplicate").json()["slug"] == "guide-copy-2"


class TestSlugHistory:
    def test_list_add_and_deactivate(self, client):
        entry = PublishedEntryFactory()
        base = f"{URL}{entry.uid}/slug-history/"
        added = client.post(base, {"slug": "older-name", "note": "2024 URL"}, format="json")
        assert added.status_code == 201 and added.json()["active"] is True
        assert OutboxEvent.objects.get(event_type="blog.content_changed").payload["paths"] == [f"{entry.collection.path_prefix}/older-name"]
        assert [row["slug"] for row in client.get(base).json()] == ["older-name"]
        assert client.post(base, {"slug": "older-name"}, format="json").json()["code"] == "slug_taken"
        assert client.post(base, {"slug": "test"}, format="json").json()["code"] == "slug_placeholder"
        alias_uid = added.json()["uid"]
        assert client.post(f"{base}{alias_uid}/deactivate/").json()["active"] is False
        assert client.post(f"{base}{alias_uid}/deactivate/").json()["active"] is False  # idempotent
        assert not EntrySlugHistory.objects.get().active

    def test_deactivate_honours_expected_version(self, client):
        alias = AliasFactory()
        url = f"{URL}{alias.entry.uid}/slug-history/{alias.uid}/deactivate/"
        stale = client.post(url, {"expected_version": alias.version + 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert EntrySlugHistory.objects.get().active
        assert client.post(url, {"expected_version": alias.version}, format="json").json()["active"] is False

    def test_bulk_retirement_is_stamped_like_any_versioned_write(self, client, editor):
        """Deleting an entry and reclaiming an old slug retire aliases in bulk: version, updated_by and updated_at move."""
        deleted = AliasFactory(slug="gone-name")
        client.delete(f"{URL}{deleted.entry.uid}/")
        deleted.refresh_from_db()
        assert not deleted.active and deleted.version == 2 and deleted.updated_by == editor
        reclaimed = AliasFactory(slug="first-name")
        entry = reclaimed.entry
        assert client.patch(f"{URL}{entry.uid}/", {"slug": "first-name"}, format="json").status_code == 200
        reclaimed.refresh_from_db()
        assert not reclaimed.active and reclaimed.version == 2 and reclaimed.updated_by == editor

    def test_alias_of_another_entry_is_404(self, client):
        alias = AliasFactory()
        other = EntryFactory()
        assert client.post(f"{URL}{other.uid}/slug-history/{alias.uid}/deactivate/").status_code == 404

    def test_viewer_can_list_but_not_add(self, auth_client, make_user):
        entry = EntryFactory()
        viewer = auth_client(make_user(grants={"blogs": ["view"]}))
        assert viewer.get(f"{URL}{entry.uid}/slug-history/").status_code == 200
        assert viewer.post(f"{URL}{entry.uid}/slug-history/", {"slug": "x-y-z"}, format="json").status_code == 403


class TestPreview:
    def test_preview_link_shows_the_current_draft(self, client, api_client, editor):
        entry = EntryFactory(title="Draft title")
        link = act(client, entry, "preview")
        assert link.status_code == 201
        url = link.json()["url"]
        assert "/api/public/v1/content/preview/" in url and AuditLog.objects.get(action="blog.entry_preview_issued").actor == editor
        response = api_client.get(url)
        assert response.status_code == 200 and response["Cache-Control"] == "private, no-store" and response["X-Robots-Tag"] == "noindex, nofollow"
        body = response.json()
        assert body["data"][0]["title"] == "Draft title" and body["meta"]["preview"] is True
        Entry.objects.filter(pk=entry.pk).update(title="Edited")
        assert api_client.get(url).json()["data"][0]["title"] == "Edited"
        assert api_client.get(f"{url}?fields[0]=title").json()["data"][0] == {"id": entry.delivery_id, "documentId": str(entry.uid), "title": "Edited"}

    def test_forged_expired_and_deleted(self, client, api_client, settings):
        settings.BLOG_PREVIEW_TTL_SECONDS = 600
        entry = EntryFactory()
        assert api_client.get("/api/public/v1/content/preview/forged-token/").json()["code"] == "signature_invalid"
        url = act(client, entry, "preview").json()["url"]
        with freeze_time(timezone.now() + dt.timedelta(seconds=601)):
            expired = api_client.get(url)
        assert expired.status_code == 410 and expired.json()["code"] == "link_expired"
        entry.soft_delete()
        assert api_client.get(url).status_code == 404

    def test_a_token_for_another_salt_is_refused(self, api_client):
        entry = EntryFactory()
        token = signing.TimestampSigner(salt="other").sign(str(entry.uid))
        assert api_client.get(f"/api/public/v1/content/preview/{token}/").status_code == 403
