"""Slug availability, slug history (aliases) and their staff operations (CMS_BLUEPRINT §6).

A slug is taken in a collection by a live entry **or** by an active alias of a live entry: handing a new article a
URL that still resolves to an older one is how two articles end up fighting over one path. A rename keeps the old
slug as an alias (:func:`record_slug_change`, the legacy four-guard algorithm): an entry reclaiming one of its own
old slugs retires that alias; an alias never shadows another entry's live slug and never steals another entry's alias.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from audit.services import record
from blog.models import Collection, Entry, EntrySlugHistory
from blog.services.common import NS_ENTRIES, django_errors
from blog.validators import MAX_SLUG_LENGTH, slug_error, validate_entry_slug
from core.errors import Conflict
from core.outbox import emit
from core.services import stamp_create
from flarize.cache_utils import bump


def slug_taken(collection: Collection, slug: str, *, exclude_entry: Entry | None = None) -> bool:
    entries = Entry.objects.filter(collection=collection, slug=slug)
    aliases = EntrySlugHistory.objects.filter(collection=collection, slug=slug, active=True, entry__deleted_at__isnull=True)
    if exclude_entry is not None:
        entries = entries.exclude(pk=exclude_entry.pk)
        aliases = aliases.exclude(entry_id=exclude_entry.pk)
    return entries.exists() or aliases.exists()


def slug_conflict() -> Conflict:
    return Conflict(
        "slug_taken",
        "This slug is already in use in this collection — by an entry or by an old slug still pointing at one.",
        errors={"slug": ["Already in use in this collection."]},
    )


def ensure_slug_usable(collection: Collection, slug: str, *, exclude_entry: Entry | None = None) -> None:
    """Validator (400 ``slug_malformed`` / ``slug_placeholder`` …) then availability (409 ``slug_taken``)."""
    try:
        validate_entry_slug(slug)
    except ValidationError as exc:
        raise django_errors(exc, "slug") from None
    if slug_taken(collection, slug, exclude_entry=exclude_entry):
        raise slug_conflict()


def check_slug(collection: Collection, candidate: str, *, exclude_entry: Entry | None = None) -> dict:
    """The editor's availability check: slugifies the input, says whether it is free and valid, suggests ``slug-N``."""
    slug = slugify(candidate or "")[:MAX_SLUG_LENGTH]
    error = slug_error(slug)
    available = bool(slug) and not slug_taken(collection, slug, exclude_entry=exclude_entry)
    suggestion = slug
    if slug and not available:
        counter = 2
        while slug_taken(collection, f"{slug}-{counter}", exclude_entry=exclude_entry):
            counter += 1
        suggestion = f"{slug}-{counter}"
    return {"slug": slug, "available": available, "suggestion": suggestion, "valid": error is None, "error": error}


def record_slug_change(entry: Entry, old_slug: str, *, user, note: str = "") -> EntrySlugHistory | None:
    """Keep ``old_slug`` pointing at ``entry`` after a rename (inside the caller's transaction)."""
    old_slug = (old_slug or "").strip()
    if not old_slug or old_slug == entry.slug:
        return None
    now = timezone.now()
    # 1. Reclaim: the entry has taken this slug back — the live row wins, retire the alias.
    EntrySlugHistory.objects.filter(collection=entry.collection, slug=entry.slug, active=True).update(active=False, updated_at=now)
    # 2. Never shadow another live entry that now holds the old slug.
    if Entry.objects.filter(collection=entry.collection, slug=old_slug).exclude(pk=entry.pk).exists():
        return None
    # 3. Never steal an existing active alias (ours already: nothing to do).
    existing = EntrySlugHistory.objects.filter(collection=entry.collection, slug=old_slug, active=True).first()
    if existing is not None:
        return existing
    # 4. Record it.
    alias = EntrySlugHistory(entry=entry, collection=entry.collection, slug=old_slug, note=note or f"renamed to '{entry.slug}'")
    stamp_create(alias, user)
    alias.save()
    return alias


def aliases_queryset(entry: Entry):
    return EntrySlugHistory.objects.filter(entry=entry).order_by("-created_at", "-id")


@transaction.atomic
def add_alias(entry: Entry, *, user, slug: str, note: str = "") -> EntrySlugHistory:
    """Record a *verified* historical slug for ``entry`` (renames that predate the alias table)."""
    entry = Entry.objects.select_for_update(of=("self",)).select_related("collection").get(pk=entry.pk)
    try:
        validate_entry_slug(slug)
    except ValidationError as exc:
        raise django_errors(exc, "slug") from None
    if slug == entry.slug or slug_taken(entry.collection, slug):
        raise slug_conflict()
    alias = EntrySlugHistory(entry=entry, collection=entry.collection, slug=slug, note=note or "verified historical slug")
    stamp_create(alias, user)
    try:
        with transaction.atomic():
            alias.save()
    except IntegrityError:
        raise slug_conflict() from None
    record("blog.entry_alias_added", obj=entry, actor=user, after={"slug": slug, "note": alias.note})
    bump(NS_ENTRIES)
    if entry.status == Entry.Status.PUBLISHED:
        emit("blog.content_changed", {"reason": "alias_added", "paths": [f"{entry.collection.path_prefix}/{slug}"]}, aggregate_type="blog.entry", aggregate_uid=entry.uid)
    return alias


@transaction.atomic
def deactivate_alias(alias: EntrySlugHistory, *, user) -> EntrySlugHistory:
    alias = EntrySlugHistory.objects.select_for_update(of=("self",)).select_related("entry", "collection").get(pk=alias.pk)
    if not alias.active:
        return alias
    alias.versioned_update(user, active=False)
    record("blog.entry_alias_deactivated", obj=alias.entry, actor=user, before={"slug": alias.slug, "active": True}, after={"slug": alias.slug, "active": False})
    bump(NS_ENTRIES)
    if alias.entry.status == Entry.Status.PUBLISHED:
        emit("blog.content_changed", {"reason": "alias_deactivated", "paths": [f"{alias.collection.path_prefix}/{alias.slug}"]}, aggregate_type="blog.entry", aggregate_uid=alias.entry.uid)
    return alias


def deactivate_entry_aliases(entry: Entry) -> None:
    """A deleted entry releases its old URLs (the alias rows stay for audit)."""
    EntrySlugHistory.objects.filter(entry=entry, active=True).update(active=False, updated_at=timezone.now())
