"""Database invariants (partial uniques, checks) and the ``blogs`` dashboard counters."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from blog.models import Entry, EntryImage, EntrySlugHistory
from blog.tests.factories import AliasFactory, AttributeValueFactory, BadgeFactory, CollectionFactory, EntryFactory, ImageGroupFactory, PublishedEntryFactory, TemplateFactory

pytestmark = pytest.mark.django_db


def violates(factory, **kwargs):
    with pytest.raises(IntegrityError), transaction.atomic():
        factory(**kwargs)


def test_entry_checks():
    violates(EntryFactory, status="PUBLISHED")  # published without published_at
    violates(EntryFactory, status="ARCHIVED")  # archived without archived_at
    violates(EntryFactory, status="BOGUS")
    violates(PublishedEntryFactory, scheduled_for=timezone.now())
    violates(EntryFactory, locale="EN")


def test_slug_is_unique_per_collection_among_live_entries():
    entry = EntryFactory(slug="same-slug")
    violates(EntryFactory, collection=entry.collection, slug="same-slug")
    EntryFactory(slug="same-slug")  # another collection
    entry.soft_delete()
    EntryFactory(collection=entry.collection, slug="same-slug")  # a deleted entry frees the slug


def test_active_alias_is_unique_per_collection():
    alias = AliasFactory(slug="old-one")
    violates(AliasFactory, entry=alias.entry, collection=alias.collection, slug="old-one")
    EntrySlugHistory.objects.filter(pk=alias.pk).update(active=False)
    AliasFactory(entry=alias.entry, collection=alias.collection, slug="old-one")


def test_image_has_exactly_one_source():
    entry = EntryFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        EntryImage.objects.create(entry=entry, group_key="coverImg", external_url="")


def test_group_max_items_and_badge_colour_checks():
    violates(ImageGroupFactory, max_items=2)  # not repeatable
    violates(ImageGroupFactory, repeatable=True, max_items=0)
    violates(BadgeFactory, color="orange")


def test_attribute_value_unique_per_slot_among_live_rows():
    value = AttributeValueFactory(slot_key="difficulty")
    violates(AttributeValueFactory, entry=value.entry, slot_key="difficulty")
    value.soft_delete()
    AttributeValueFactory(entry=value.entry, slot_key="difficulty")


def test_collection_and_template_unique_among_live_rows():
    collection = CollectionFactory(api_uid="articles")
    violates(CollectionFactory, api_uid="articles")
    collection.soft_delete()
    CollectionFactory(api_uid="articles")
    TemplateFactory(slug="guide")
    violates(TemplateFactory, slug="guide")


def test_dashboard_counters(auth_client, make_user):
    EntryFactory()
    EntryFactory(status=Entry.Status.REVIEW, scheduled_for=timezone.now())
    PublishedEntryFactory()
    EntryFactory(status=Entry.Status.ARCHIVED, archived_at=timezone.now())
    body = auth_client(make_user(grants={"blogs": ["view"], "dashboard": ["view"]})).get("/api/v1/dashboard/").json()
    counts = body["modules"]["blogs"]
    assert counts == {"entries_draft": 1, "entries_review": 1, "entries_published": 1, "entries_archived": 1, "entries_scheduled": 1}
