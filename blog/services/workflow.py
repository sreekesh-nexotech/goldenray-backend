"""Entry lifecycle (POST actions, never a PATCH of ``status``): submit, publish, unpublish, schedule, archive, restore,
verify, duplicate — and the Beat task that publishes due scheduled entries.

=============  ===========================  =======================  ==============================================
action         from                         to                       effects
=============  ===========================  =======================  ==============================================
submit         DRAFT                        REVIEW
publish        DRAFT, REVIEW                PUBLISHED                publish-time validation; ``published_at`` set
                                                                     once, ``published_on`` seeded; schedule cleared;
                                                                     ``blog.entry_published``
unpublish      PUBLISHED                    DRAFT                    dates kept; ``blog.entry_unpublished``
schedule       DRAFT, REVIEW                (unchanged)              validated now; ``scheduled_for`` (null cancels)
archive        DRAFT, REVIEW, PUBLISHED     ARCHIVED                 ``archived_at``; ``blog.entry_archived`` if public
restore        ARCHIVED                     DRAFT                    re-publishing stays a separate step
verify         any but ARCHIVED             (unchanged)              ``verified_by``/``verified_at``
duplicate      any                          new DRAFT                ``-copy`` slug; dates, schedule, verification and
                                                                     archive stamp reset (legacy weakness §17 #6)
=============  ===========================  =======================  ==============================================

Authorisation is the view's job (``HasModulePermission``); the services take the acting ``user`` as a required keyword
and the Beat task passes ``user=None`` explicitly under a ``SYSTEM`` audit context (legacy weakness §17 #7).
"""

from __future__ import annotations

import logging
from collections import Counter

from django.db import transaction
from django.utils import timezone

from audit.context import bind
from audit.services import record
from blog.models import Entry
from blog.services.attributes import EMPTY_VALUES, coerce_value
from blog.services.common import NS_ENTRIES, SEQ_BLOCK, SEQ_ENTRY, next_delivery_id
from blog.services.entries import live_seo, publication_event, write_attributes, write_blocks, write_images, write_links, write_seo
from blog.services.slugs import slug_taken
from blog.validators import slug_error
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

logger = logging.getLogger("flarize.blog")
Status = Entry.Status


def _lock(entry: Entry, expected_version) -> Entry:
    locked = Entry.objects.select_for_update(of=("self",)).select_related("collection", "template").get(pk=entry.pk)
    check_version(locked, expected_version)
    return locked


def _require(entry: Entry, allowed: tuple[str, ...], action: str) -> None:
    if entry.status not in allowed:
        raise Conflict("invalid_transition", f"Cannot {action} an entry that is {entry.get_status_display().lower()}.", errors={"status": [f"Allowed from: {', '.join(allowed)}."]})


def publish_problems(entry: Entry) -> dict[str, list[str]]:
    """Publish-time validation against the template (legacy ``validate_for_publish``, plus value types)."""
    problems: dict[str, list[str]] = {}
    if not (entry.title or "").strip():
        problems.setdefault("title", []).append("A title is required.")
    error = slug_error(entry.slug)
    if error:
        problems.setdefault("slug", []).append(error)
    template = entry.template
    if template is None or template.deleted_at is not None:
        return problems
    provided = {value.slot_key: value.value for value in entry.attribute_values.all()}
    for slot in template.attribute_slots.all():
        value = provided.get(slot.key)
        if slot.required and value in EMPTY_VALUES:
            problems.setdefault("attribute_values", []).append(f"Attribute '{slot.label}' ({slot.key}) is required.")
        elif value is not None:
            try:
                coerce_value(slot, value)
            except ValueError as exc:
                problems.setdefault("attribute_values", []).append(f"Attribute '{slot.label}' ({slot.key}) {exc}.")
    counts = Counter(image.group_key for image in entry.images.all())
    for group in template.image_groups.all():
        count = counts.get(group.key, 0)
        if group.required and count == 0:
            problems.setdefault("images", []).append(f"Image group '{group.label}' ({group.key}) requires at least one image.")
        if not group.repeatable and count > 1:
            problems.setdefault("images", []).append(f"Image group '{group.label}' ({group.key}) is single but has {count} images.")
        if group.repeatable and group.max_items is not None and count > group.max_items:
            problems.setdefault("images", []).append(f"Image group '{group.label}' ({group.key}) exceeds max_items={group.max_items}.")
    return problems


def validate_for_publish(entry: Entry) -> None:
    problems = publish_problems(entry)
    if problems:
        messages = [message for items in problems.values() for message in items]
        raise DomainError("publish_validation_failed", " ".join(messages), errors=problems)


def _audit(action: str, entry: Entry, user, before: dict, after: dict) -> None:
    record(f"blog.entry_{action}", obj=entry, actor=user, before=before, after=after)
    bump(NS_ENTRIES)


@transaction.atomic
def submit_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    entry = _lock(entry, expected_version)
    _require(entry, (Status.DRAFT,), "submit")
    entry.versioned_update(user, status=Status.REVIEW)
    _audit("submitted", entry, user, {"status": Status.DRAFT}, {"status": Status.REVIEW})
    return entry


def _publish_locked(entry: Entry, *, user) -> Entry:
    _require(entry, (Status.DRAFT, Status.REVIEW), "publish")
    validate_for_publish(entry)
    now = timezone.now()
    published_at = entry.published_at or now
    before = {"status": entry.status, "scheduled_for": entry.scheduled_for}
    entry.versioned_update(user, status=Status.PUBLISHED, published_at=published_at, published_on=entry.published_on or published_at, scheduled_for=None, archived_at=None)
    _audit("published", entry, user, before, {"status": entry.status, "published_at": entry.published_at, "published_on": entry.published_on})
    publication_event(entry, "blog.entry_published")
    return entry


@transaction.atomic
def publish_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    return _publish_locked(_lock(entry, expected_version), user=user)


@transaction.atomic
def unpublish_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    entry = _lock(entry, expected_version)
    _require(entry, (Status.PUBLISHED,), "unpublish")
    entry.versioned_update(user, status=Status.DRAFT)
    _audit("unpublished", entry, user, {"status": Status.PUBLISHED}, {"status": Status.DRAFT})
    publication_event(entry, "blog.entry_unpublished")
    return entry


@transaction.atomic
def schedule_entry(entry: Entry, *, user, scheduled_for, expected_version=None) -> Entry:
    """Publish at ``scheduled_for`` (validated now, so a broken entry fails today, not at midnight); ``None`` cancels."""
    entry = _lock(entry, expected_version)
    _require(entry, (Status.DRAFT, Status.REVIEW), "schedule")
    if scheduled_for is not None:
        if scheduled_for <= timezone.now():
            raise DomainError("schedule_in_past", "The publication time must be in the future.", errors={"scheduled_for": ["Must be in the future."]})
        validate_for_publish(entry)
    if scheduled_for == entry.scheduled_for:
        return entry
    before = {"scheduled_for": entry.scheduled_for}
    entry.versioned_update(user, scheduled_for=scheduled_for)
    _audit("scheduled" if scheduled_for else "unscheduled", entry, user, before, {"scheduled_for": scheduled_for})
    return entry


@transaction.atomic
def archive_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    entry = _lock(entry, expected_version)
    _require(entry, (Status.DRAFT, Status.REVIEW, Status.PUBLISHED), "archive")
    was_public = entry.status == Status.PUBLISHED
    before = {"status": entry.status}
    entry.versioned_update(user, status=Status.ARCHIVED, archived_at=timezone.now(), scheduled_for=None)
    _audit("archived", entry, user, before, {"status": Status.ARCHIVED})
    if was_public:
        publication_event(entry, "blog.entry_archived")
    return entry


@transaction.atomic
def restore_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    entry = _lock(entry, expected_version)
    _require(entry, (Status.ARCHIVED,), "restore")
    entry.versioned_update(user, status=Status.DRAFT, archived_at=None)
    _audit("restored", entry, user, {"status": Status.ARCHIVED}, {"status": Status.DRAFT})
    return entry


@transaction.atomic
def verify_entry(entry: Entry, *, user, expected_version=None) -> Entry:
    entry = _lock(entry, expected_version)
    _require(entry, (Status.DRAFT, Status.REVIEW, Status.PUBLISHED), "verify")
    entry.versioned_update(user, verified_by=user, verified_at=timezone.now())
    _audit("verified", entry, user, {}, {"verified_at": entry.verified_at})
    return entry


def _copy_slug(entry: Entry) -> str:
    base = f"{entry.slug}-copy"[:250]
    candidate, counter = base, 2
    while slug_taken(entry.collection, candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


@transaction.atomic
def duplicate_entry(entry: Entry, *, user) -> Entry:
    """A fresh DRAFT copy (children, links and SEO included) with a free ``-copy`` slug."""
    source = Entry.objects.select_related("collection", "template").get(pk=entry.pk)
    copy = Entry(
        delivery_id=next_delivery_id(SEQ_ENTRY),
        collection=source.collection,
        template=source.template,
        title=f"{source.title} (copy)"[:255],
        slug=_copy_slug(source),
        status=Status.DRAFT,
        **{name: getattr(source, name) for name in ("excerpt", "summary", "introduction", "warning", "insights", "read_time", "is_featured", "sort_order", "locale", "author", "cover_image")},
    )
    stamp_create(copy, user)
    copy.save()
    write_links(copy, "categories", list(source.categories.all()))
    write_links(copy, "tags", list(source.tags.all()))
    write_links(copy, "badges", list(source.badges.all()))
    blocks = [{"kind": block.kind, "component": block.component, "data": block.data, "position": block.position} for block in source.content_blocks.order_by("position", "delivery_id")]
    write_blocks(copy, blocks, user, delivery_ids=[next_delivery_id(SEQ_BLOCK) for _ in blocks])
    write_images(
        copy, [{name: getattr(image, name) for name in ("group_key", "position", "media_asset", "external_url", "alt")} for image in source.images.order_by("group_key", "position", "id")], user
    )
    write_attributes(copy, [{"slot_key": value.slot_key, "value": value.value} for value in source.attribute_values.all()], user)
    seo = live_seo(source)
    if seo is not None:
        write_seo(
            copy,
            {name: getattr(seo, name) for name in ("seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex", "keywords")},
            user,
        )
    record("blog.entry_duplicated", obj=copy, actor=user, after={"source": str(source.uid), "slug": copy.slug})
    bump(NS_ENTRIES)
    return copy


def publish_due_entries(now=None) -> dict[str, int]:
    """Publish every live DRAFT/REVIEW entry whose ``scheduled_for`` has passed (Beat, every minute).

    Each entry is published in its own transaction under a ``SYSTEM`` audit context. An entry that no longer passes
    publish validation is not retried every minute: its schedule is cleared and the failure audited.
    """
    now = now or timezone.now()
    counts = {"published": 0, "failed": 0}
    due = list(Entry.objects.filter(status__in=[Status.DRAFT, Status.REVIEW], scheduled_for__lte=now).order_by("scheduled_for", "id").values_list("pk", flat=True))
    with bind(actor=None, actor_kind="SYSTEM"):
        for pk in due:
            try:
                with transaction.atomic():
                    entry = (
                        Entry.objects.select_for_update(skip_locked=True, of=("self",))
                        .select_related("collection", "template")
                        .filter(pk=pk, scheduled_for__lte=now, status__in=[Status.DRAFT, Status.REVIEW])
                        .first()
                    )
                    if entry is None:
                        continue
                    _publish_locked(entry, user=None)
                    counts["published"] += 1
            except DomainError as exc:
                counts["failed"] += 1
                logger.warning("scheduled publication failed", extra={"entry_pk": pk, "code": exc.code})
                with transaction.atomic():
                    entry = Entry.objects.select_for_update().get(pk=pk)
                    scheduled_for = entry.scheduled_for
                    entry.versioned_update(None, scheduled_for=None)
                    record("blog.entry_schedule_failed", obj=entry, actor=None, actor_kind="SYSTEM", before={"scheduled_for": scheduled_for}, after={"code": exc.code, "errors": exc.errors})
                    bump(NS_ENTRIES)
    return counts
