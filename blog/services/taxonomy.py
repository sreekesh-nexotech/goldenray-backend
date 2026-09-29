"""Authors, categories, tags and badges (staff ``content/authors/`` … ``content/badges/``).

Slugs are generated from the name when omitted (``solar-basics``, ``solar-basics-2`` …) and validated when given.
Each row gets its public ``delivery_id`` from a ``core.sequences`` counter (DV-34). A term used by a live entry
cannot be deleted (409 ``<kind>_in_use``): the delivery payload never points at a deleted record. Renaming a term
that published entries show emits ``blog.content_changed`` so those pages are revalidated.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from audit.services import changes, record, snapshot
from blog.models import Author, Badge, Category, Entry, Tag
from blog.services.common import (
    NS_AUTHORS,
    NS_BADGES,
    NS_CATEGORIES,
    NS_TAGS,
    SEQ_AUTHOR,
    SEQ_BADGE,
    SEQ_CATEGORY,
    SEQ_TAG,
    emit_content_changed,
    in_use,
    invalid,
    next_delivery_id,
    unique_slug,
    validate_simple_slug,
)
from blog.validators import COLOR_RE
from core.errors import Conflict
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from media.models import MediaAsset


@dataclass(frozen=True)
class Kind:
    model: type
    name: str  # audit/event noun
    fields: tuple[str, ...]
    namespace: str
    sequence: str
    entry_lookup: str  # Entry filter reaching this term
    slug_max: int
    shown_fields: tuple[str, ...]  # fields the delivery payload shows (a change revalidates published pages)


AUTHOR = Kind(Author, "author", ("name", "slug", "role", "bio", "avatar", "user"), NS_AUTHORS, SEQ_AUTHOR, "author", 160, ("name", "role", "bio"))
CATEGORY = Kind(Category, "category", ("name", "slug"), NS_CATEGORIES, SEQ_CATEGORY, "categories", 120, ("name", "slug"))
TAG = Kind(Tag, "tag", ("name", "slug"), NS_TAGS, SEQ_TAG, "tags", 80, ("name",))
BADGE = Kind(Badge, "badge", ("name", "slug", "color"), NS_BADGES, SEQ_BADGE, "badges", 120, ("name", "color"))


def queryset(kind: Kind):
    live_entries = Q(entries__deleted_at__isnull=True)
    qs = kind.model.objects.annotate(entry_count=Count("entries", filter=live_entries, distinct=True))
    if kind is AUTHOR:
        qs = qs.select_related("avatar", "user")
    return qs.order_by("name", "id")


def _validate(kind: Kind, values: dict, current=None) -> dict:
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise invalid("name", "A name is required.")
    if values.get("slug"):
        validate_simple_slug(values["slug"])
    if "color" in values and not COLOR_RE.match(values["color"] or ""):
        raise invalid("color", "A hex colour such as '#ED8723'.", "color_malformed")
    avatar = values.get("avatar")
    if avatar is not None and (avatar.deleted_at is not None or avatar.kind != MediaAsset.Kind.IMAGE or not avatar.is_public):
        raise invalid("avatar_uid", "The avatar must be a public image.", "invalid_media")
    return values


def _conflict(kind: Kind) -> Conflict:
    field = "name" if kind is TAG else "slug"
    return Conflict(f"{kind.name}_taken", f"Another {kind.name} already uses this {field}.", errors={field: ["Already in use."]})


def _entries_using(kind: Kind, term):
    return Entry.objects.filter(**{kind.entry_lookup: term})


@transaction.atomic
def create(kind: Kind, *, user, data):
    values = _validate(kind, {name: data[name] for name in kind.fields if name in data})
    if not values.get("slug"):
        values["slug"] = unique_slug(kind.model.objects.all(), values["name"], fallback=kind.name, max_length=kind.slug_max)
    term = kind.model(delivery_id=next_delivery_id(kind.sequence), **values)
    stamp_create(term, user)
    try:
        with transaction.atomic():
            term.save()
    except IntegrityError:
        raise _conflict(kind) from None
    record(f"blog.{kind.name}_created", obj=term, actor=user, after=snapshot(term, kind.fields))
    bump(kind.namespace)
    return term


@transaction.atomic
def update(kind: Kind, instance, *, user, data, expected_version=None):
    term = kind.model.objects.select_for_update().get(pk=instance.pk)
    check_version(term, expected_version)
    values = _validate(kind, {name: data[name] for name in kind.fields if name in data}, term)
    if "slug" in values and not values["slug"]:
        values["slug"] = unique_slug(kind.model.objects.all(), values.get("name", term.name), exclude_pk=term.pk, fallback=kind.name, max_length=kind.slug_max)
    values = {name: value for name, value in values.items() if value != getattr(term, name)}
    if not values:
        return term
    before = snapshot(term, kind.fields)
    try:
        with transaction.atomic():
            term.versioned_update(user, **values)
    except IntegrityError:
        raise _conflict(kind) from None
    changed_before, changed_after = changes(before, snapshot(term, kind.fields))
    record(f"blog.{kind.name}_updated", obj=term, actor=user, before=changed_before, after=changed_after)
    bump(kind.namespace)
    if set(values) & set(kind.shown_fields):
        emit_content_changed(_entries_using(kind, term), reason=f"{kind.name}_updated")
    return term


@transaction.atomic
def delete(kind: Kind, instance, *, user, expected_version=None) -> None:
    term = kind.model.objects.select_for_update().get(pk=instance.pk)
    check_version(term, expected_version)
    count = _entries_using(kind, term).count()
    if count:
        raise in_use(f"{kind.name}_in_use", kind.name, count)
    term.soft_delete(user)
    record(f"blog.{kind.name}_deleted", obj=term, actor=user, before=snapshot(term, kind.fields))
    bump(kind.namespace)


# Service maps for the viewsets (core.views.mixins call services["create"](user=, data=) etc.).
def services_for(kind: Kind) -> dict:
    return {
        "create": lambda *, user, data: create(kind, user=user, data=data),
        "update": lambda instance, *, user, data, expected_version=None: update(kind, instance, user=user, data=data, expected_version=expected_version),
        "destroy": lambda instance, *, user, expected_version=None: delete(kind, instance, user=user, expected_version=expected_version),
    }
