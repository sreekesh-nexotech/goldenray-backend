"""Shared helpers of the blog services: cache namespaces, public numbers, slugs, errors and revalidation events."""

from __future__ import annotations

import re
from collections.abc import Iterable

from django.core.exceptions import ValidationError
from django.utils.text import slugify

from core import sequences
from core.errors import Conflict, DomainError
from core.outbox import emit

# Version-keyed cache namespaces (flarize.cache_utils): one per model the delivery payload embeds (standard §7.1).
NS_COLLECTIONS = "blog:collections"
NS_TEMPLATES = "blog:templates"
NS_ENTRIES = "blog:entries"
NS_AUTHORS = "blog:authors"
NS_CATEGORIES = "blog:categories"
NS_TAGS = "blog:tags"
NS_BADGES = "blog:badges"
DELIVERY_NAMESPACES = (NS_COLLECTIONS, NS_TEMPLATES, NS_ENTRIES, NS_AUTHORS, NS_CATEGORIES, NS_TAGS, NS_BADGES, "media")

# core.sequences kinds for the public numeric ids of the delivery contract (DV-34).
SEQ_ENTRY = "BLOG_ENTRY"
SEQ_AUTHOR = "BLOG_AUTHOR"
SEQ_CATEGORY = "BLOG_CATEGORY"
SEQ_TAG = "BLOG_TAG"
SEQ_BADGE = "BLOG_BADGE"
SEQ_BLOCK = "BLOG_BLOCK"

# A revalidation event lists at most this many entry paths (plus index paths); beyond that the ISR window catches up.
MAX_REVALIDATE_PATHS = 100
_SIMPLE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def next_delivery_id(kind: str) -> int:
    """The next public number of ``kind`` (inside the caller's transaction)."""
    return sequences.next_value(kind)


def invalid(field: str, message: str, code: str = "validation_error") -> DomainError:
    return DomainError(code, message, errors={field: [message]})


def django_errors(exc: ValidationError, field: str) -> DomainError:
    messages = list(exc.messages)
    code = getattr(exc, "code", None) or (exc.error_list[0].code if getattr(exc, "error_list", None) else None) or "validation_error"
    return DomainError(code, messages[0] if messages else "Invalid value.", errors={field: messages})


def validate_simple_slug(value: str, field: str = "slug") -> str:
    if not value or len(value) > 160 or not _SIMPLE_SLUG_RE.match(value):
        raise invalid(field, "Use lowercase letters, numbers and single hyphens (e.g. 'solar-basics').", "slug_malformed")
    return value


def unique_slug(queryset, base: str, *, field: str = "slug", exclude_pk=None, fallback: str = "item", max_length: int = 120) -> str:
    """``base`` slugified, suffixed ``-2``, ``-3`` … until no live row of ``queryset`` uses it."""
    root = (slugify(base) or fallback)[: max_length - 6].strip("-") or fallback
    candidate, counter = root, 2
    existing = queryset.exclude(pk=exclude_pk) if exclude_pk else queryset
    while existing.filter(**{field: candidate}).exists():
        candidate = f"{root}-{counter}"
        counter += 1
    return candidate


def in_use(code: str, what: str, count: int) -> Conflict:
    return Conflict(code, f"This {what} is used by {count} entr{'y' if count == 1 else 'ies'}; detach it first.", errors={"entries": [f"{count} live entries reference it."]})


def entry_paths(entries: Iterable) -> list[str]:
    """Revalidation paths of published entries (their collection index, their page and — when ``active_aliases`` is
    prefetched — the old URLs the website also renders them under), capped."""
    paths: list[str] = []
    for entry in entries:
        prefix = entry.collection.path_prefix
        aliases = [alias.slug for alias in getattr(entry, "active_aliases", ())]
        for path in (prefix, *(f"{prefix}/{slug}" for slug in (entry.slug, *aliases))):
            if path not in paths:
                paths.append(path)
        if len(paths) >= MAX_REVALIDATE_PATHS:
            break
    return paths[:MAX_REVALIDATE_PATHS]


def emit_content_changed(entries_queryset, *, reason: str, extra_paths: Iterable[str] = ()) -> None:
    """``blog.content_changed``: a shared record (author, category, template …) changed what published pages show."""
    from django.db.models import Prefetch

    from blog.models import Entry, EntrySlugHistory

    aliases = Prefetch("slug_history", queryset=EntrySlugHistory.objects.filter(active=True).order_by("created_at", "id"), to_attr="active_aliases")
    published = entries_queryset.filter(status=Entry.Status.PUBLISHED, deleted_at__isnull=True).select_related("collection").prefetch_related(aliases).order_by("delivery_id")[:MAX_REVALIDATE_PATHS]
    paths = entry_paths(published)
    for path in extra_paths:
        if path not in paths:
            paths.append(path)
    if paths:
        emit("blog.content_changed", {"reason": reason, "paths": paths}, aggregate_type="blog.entry")
