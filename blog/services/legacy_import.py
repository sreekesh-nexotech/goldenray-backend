"""Idempotent importers for the legacy CMS blog tables (PLAN §7.2 rows 4–7): ``catalog_*`` and ``content_*``.

Each function takes plain row dicts (what a read-only source cursor or the committed JSON fixture yields) and returns
``{"created", "updated", "skipped", "violations"}``. ``core_legacy_map`` makes them idempotent and traceable:
re-running updates the mapped rows and never duplicates (``skipped`` counts rows already up to date or refused;
``violations`` lists every refused row and every lossy transform). Each call writes one ``blog.legacy_import``
audit row with the counts and the source checksum (PLAN §7.6 #12).

Rules:

* primary keys of authors, categories, tags, badges, entries and content blocks become their public ``delivery_id``
  (the Strapi ``id`` the website reads, DV-17); the counters continue after the highest imported value;
* ``content_entry.document_id`` becomes ``uid`` unchanged (the frontend's ``documentId``); ``created_at`` /
  ``updated_at`` / ``published_at`` / ``published_on`` are preserved, so the delivery payload is byte-identical;
* status map: ``draft`` → DRAFT, ``published`` → PUBLISHED, ``archived`` and ``deleted`` → ARCHIVED (the CMS
  "deleted" state is a take-down that keeps the slug claimed; ``archived_at`` = its ``deleted_at``), any review state
  → REVIEW; unknown values → DRAFT (violation);
* slugs are imported verbatim (they are live URLs) — values failing today's validator are reported, not rewritten;
  a ``(collection, slug)`` clash with an unrelated entry refuses the row;
* media references (``cover_image_id``, ``media_asset_id``) resolve through ``media_map`` (legacy id → ``MediaAsset``)
  or, by default, the media import's ``core_legacy_map`` rows (``CMS``/``media_asset``); unresolved references are
  dropped and reported (an image row left without a source is refused);
* ``created_by``/``updated_by`` resolve through ``user_map`` (legacy admin-user id → ``accounts.User``);
* attribute values are coerced to their slot type (lenient: ``"6"`` → 6 for a number slot); values that do not fit
  are kept verbatim and reported.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Model
from django.utils.dateparse import parse_datetime

from audit.services import record
from blog.models import (
    Author,
    Badge,
    Category,
    Collection,
    ContentBlock,
    Entry,
    EntryAttributeValue,
    EntryBadge,
    EntryCategory,
    EntryImage,
    EntrySeo,
    EntrySlugHistory,
    EntryTag,
    Tag,
    Template,
    TemplateAttributeSlot,
    TemplateImageGroup,
)
from blog.services.attributes import LEGACY_TYPES, coerce_value, validate_slot_options
from blog.services.common import DELIVERY_NAMESPACES, SEQ_AUTHOR, SEQ_BADGE, SEQ_BLOCK, SEQ_CATEGORY, SEQ_ENTRY, SEQ_TAG, next_delivery_id, unique_slug
from blog.services.entries import DEFAULT_COMPONENTS, KIND_BY_COMPONENT
from blog.validators import COLOR_RE, COMPONENT_RE, slug_error, validate_api_uid
from core import sequences
from core.errors import DomainError
from core.models import LegacyMap
from flarize.cache_utils import bump
from media.models import MediaAsset

SOURCE = LegacyMap.SourceSystem.CMS
DEFAULT_PATH_PREFIXES = {"articles": "/blog"}
STATUS_MAP = {
    "draft": Entry.Status.DRAFT,
    "published": Entry.Status.PUBLISHED,
    "archived": Entry.Status.ARCHIVED,
    "deleted": Entry.Status.ARCHIVED,
    "review": Entry.Status.REVIEW,
    "in_review": Entry.Status.REVIEW,
    "pending_review": Entry.Status.REVIEW,
    "pending": Entry.Status.REVIEW,
}
MAX_SMALLINT = 32767


@dataclass
class Report:
    table: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    violations: list[dict] = field(default_factory=list)

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"table": self.table, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _dt(value):
    if value is None or isinstance(value, dt.datetime):
        return value
    return parse_datetime(str(value))


def _text(value) -> str:
    return "" if value is None else str(value)


def checksum(rows: Iterable[Mapping]) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, default=str).encode()).hexdigest()


def mapped(source_table: str, source_id, model: type[Model]):
    """The platform row a legacy row was imported into (soft-deleted rows included), or ``None``."""
    target = LegacyMap.objects.filter(source_system=SOURCE, source_table=source_table, source_id=str(source_id)).values_list("target_id", flat=True).first()
    if target is None:
        return None
    manager = getattr(model, "all_objects", model._default_manager)
    return manager.filter(pk=target).first()


def remember(source_table: str, source_id, instance: Model) -> None:
    LegacyMap.objects.update_or_create(source_system=SOURCE, source_table=source_table, source_id=str(source_id), defaults={"target_table": instance._meta.db_table, "target_id": instance.pk})


def _resolver(source_table: str, model: type[Model], explicit: Mapping | None) -> Callable:
    def resolve(source_id):
        if source_id is None:
            return None
        if explicit is not None:
            return explicit.get(source_id, explicit.get(str(source_id)))
        return mapped(source_table, source_id, model)

    return resolve


def _user(user_map: Mapping | None, source_id, report: Report, row_id, column: str):
    if source_id is None:
        return None
    user = (user_map or {}).get(source_id, (user_map or {}).get(str(source_id)))
    if user is None:
        report.violation(row_id, "user_unmapped", f"{column} {source_id} has no platform user; attribution dropped.")
    return user


def _media(resolve: Callable, source_id, report: Report, row_id, column: str):
    if source_id is None:
        return None
    asset = resolve(source_id)
    if asset is None:
        report.violation(row_id, "media_unmapped", f"{column} {source_id} was not imported into media; reference dropped.")
    elif asset.deleted_at is not None or not asset.is_public:
        report.violation(row_id, "media_not_public", f"{column} {source_id} is not a live public asset; reference dropped.")
        return None
    return asset


def upsert(report: Report, source_table: str, row_id, model: type[Model], values: dict, *, create_only: dict | None = None, preserve: dict | None = None):
    """Create or update the row mapped to ``(source_table, row_id)``; ``preserve`` columns are written verbatim."""
    instance = mapped(source_table, row_id, model)
    if instance is None:
        instance = model(**values, **(create_only or {}))
        instance.save()
        remember(source_table, row_id, instance)
        report.created += 1
    else:
        changed = {name: value for name, value in values.items() if _column(instance, name) != _column_value(model, name, value)}
        if changed:
            model.all_objects.filter(pk=instance.pk).update(**changed, version=F("version") + 1)
            report.updated += 1
        else:
            report.skipped += 1
    if preserve:
        model.all_objects.filter(pk=instance.pk).update(**preserve)
    return model.all_objects.get(pk=instance.pk)


def _column(instance, name):
    field_ = instance._meta.get_field(name)
    return getattr(instance, field_.attname)


def _column_value(model, name, value):
    field_ = model._meta.get_field(name)
    return value.pk if field_.is_relation and isinstance(value, Model) else value


def _delivery_id(model: type[Model], wanted: int, sequence: str, report: Report, row_id, reserved: set) -> int:
    """The legacy primary key as public number; a clash gets the next free number outside this batch's legacy ids."""
    if not model.all_objects.filter(delivery_id=wanted).exists():
        return int(wanted)
    sequences.ensure_next_value_at_least(sequence, max([*reserved, 0]) + 1)
    candidate = next_delivery_id(sequence)
    while model.all_objects.filter(delivery_id=candidate).exists():
        candidate = next_delivery_id(sequence)
    report.violation(row_id, "delivery_id_taken", f"Public id {wanted} is already used; {candidate} was assigned.")
    return candidate


def _continue_sequence(model: type[Model], sequence: str) -> None:
    highest = model.all_objects.aggregate(top=Max("delivery_id"))["top"]
    if highest:
        sequences.ensure_next_value_at_least(sequence, highest + 1)


def _finish(report: Report, rows: list, *, user=None) -> dict:
    record(
        "blog.legacy_import",
        object_type="blog.legacyimport",
        actor=user,
        after={"table": report.table, "created": report.created, "updated": report.updated, "skipped": report.skipped, "violations": len(report.violations), "checksum": checksum(rows)},
    )
    bump(*DELIVERY_NAMESPACES[:-1])
    return report.as_dict()


# ── Schema ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@transaction.atomic
def import_collections(rows, *, path_prefixes: Mapping[str, str] | None = None, user=None) -> dict:
    report, rows = Report("catalog_collection"), list(rows)
    prefixes = {**DEFAULT_PATH_PREFIXES, **(path_prefixes or {})}
    for row in rows:
        api_uid = _text(row.get("api_uid"))
        try:
            validate_api_uid(api_uid)
        except ValidationError as exc:
            report.violation(row["id"], "api_uid_invalid", f"'{api_uid}': {exc.messages[0]}")
            report.skipped += 1
            continue
        values = {
            "api_uid": api_uid,
            "singular_name": _text(row.get("singular_name"))[:80],
            "plural_name": _text(row.get("plural_name"))[:80],
            "description": _text(row.get("description")),
            "is_active": bool(row.get("is_active", True)),
        }
        existing = mapped(report.table, row["id"], Collection)
        create_only = {"path_prefix": prefixes.get(api_uid, f"/{api_uid}")} if existing is None else None
        upsert(report, report.table, row["id"], Collection, values, create_only=create_only, preserve=_stamps(row))
    return _finish(report, rows, user=user)


def _stamps(row) -> dict:
    stamps = {}
    for name in ("created_at", "updated_at"):
        if row.get(name):
            stamps[name] = _dt(row[name])
    return stamps


@transaction.atomic
def import_templates(rows, *, user=None) -> dict:
    report, rows = Report("catalog_template"), list(rows)
    for row in rows:
        values = {
            "slug": _text(row.get("slug"))[:120],
            "name": _text(row.get("name"))[:120],
            "description": _text(row.get("description")),
            "is_active": bool(row.get("is_active", True)),
            "sort_order": int(row.get("sort_order") or 0),
        }
        upsert(report, report.table, row["id"], Template, values, preserve=_stamps(row))
    return _finish(report, rows, user=user)


@transaction.atomic
def import_template_image_groups(rows, *, user=None) -> dict:
    report, rows = Report("catalog_template_image_group"), list(rows)
    for row in rows:
        template = mapped("catalog_template", row.get("template_id"), Template)
        if template is None:
            report.violation(row["id"], "template_unmapped", f"template {row.get('template_id')} was not imported.")
            report.skipped += 1
            continue
        repeatable, max_items = bool(row.get("repeatable")), row.get("max_items")
        if max_items is not None and (not repeatable or max_items < 1):
            report.violation(row["id"], "max_items_dropped", f"max_items={max_items} is only meaningful on a repeatable group; dropped.")
            max_items = None
        values = {
            "template": template,
            "key": _text(row.get("key"))[:60],
            "label": _text(row.get("label"))[:120],
            "repeatable": repeatable,
            "max_items": max_items,
            "required": bool(row.get("required")),
            "position": int(row.get("order") or 0),
        }
        upsert(report, report.table, row["id"], TemplateImageGroup, values)
    return _finish(report, rows, user=user)


@transaction.atomic
def import_template_attribute_slots(rows, *, user=None) -> dict:
    report, rows = Report("catalog_template_attribute_slot"), list(rows)
    for row in rows:
        template = mapped("catalog_template", row.get("template_id"), Template)
        if template is None:
            report.violation(row["id"], "template_unmapped", f"template {row.get('template_id')} was not imported.")
            report.skipped += 1
            continue
        slot_type = LEGACY_TYPES.get(_text(row.get("type")).lower())
        if slot_type is None:
            report.violation(row["id"], "type_unknown", f"type '{row.get('type')}' is unknown; imported as TEXT.")
            slot_type = TemplateAttributeSlot.Type.TEXT
        options = row.get("options") or {}
        try:
            options = validate_slot_options(slot_type, options)
        except DomainError as exc:
            report.violation(row["id"], "options_invalid", f"{exc.message} Kept verbatim.")
        values = {
            "template": template,
            "key": _text(row.get("key"))[:60],
            "label": _text(row.get("label"))[:120],
            "type": slot_type,
            "options": options,
            "required": bool(row.get("required")),
            "position": int(row.get("order") or 0),
        }
        upsert(report, report.table, row["id"], TemplateAttributeSlot, values)
    return _finish(report, rows, user=user)


# ── Taxonomy ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _import_terms(report: Report, rows: list, model: type[Model], sequence: str, build: Callable[[dict], dict]) -> None:
    reserved = {row["id"] for row in rows}
    for row in rows:
        values = build(row)
        existing = mapped(report.table, row["id"], model)
        create_only = None
        if existing is None:
            create_only = {"delivery_id": _delivery_id(model, row["id"], sequence, report, row["id"], reserved)}
            if "slug" not in values:
                create_only["slug"] = unique_slug(model.objects.all(), values["name"], fallback=model._meta.model_name)
        upsert(report, report.table, row["id"], model, values, create_only=create_only)
    _continue_sequence(model, sequence)


@transaction.atomic
def import_authors(rows, *, user=None) -> dict:
    report, rows = Report("catalog_author"), list(rows)
    _import_terms(report, rows, Author, SEQ_AUTHOR, lambda row: {"name": _text(row.get("name"))[:160], "role": _text(row.get("role"))[:120], "bio": _text(row.get("bio"))})
    return _finish(report, rows, user=user)


@transaction.atomic
def import_categories(rows, *, user=None) -> dict:
    report, rows = Report("catalog_category"), list(rows)

    def build(row):
        values = {"name": _text(row.get("name"))[:120]}
        slug = _text(row.get("slug"))
        if slug:
            values["slug"] = slug[:120]
        elif mapped(report.table, row["id"], Category) is None:
            report.violation(row["id"], "slug_generated", f"category '{values['name']}' had no slug; one was generated from the name.")
        return values

    _import_terms(report, rows, Category, SEQ_CATEGORY, build)
    return _finish(report, rows, user=user)


@transaction.atomic
def import_tags(rows, *, user=None) -> dict:
    report, rows = Report("catalog_tag"), list(rows)
    _import_terms(report, rows, Tag, SEQ_TAG, lambda row: {"name": _text(row.get("name"))[:80]})
    return _finish(report, rows, user=user)


@transaction.atomic
def import_badges(rows, *, user=None) -> dict:
    report, rows = Report("catalog_badge"), list(rows)

    def build(row):
        color = _text(row.get("color")) or "#123532"
        if not COLOR_RE.match(color):
            report.violation(row["id"], "color_invalid", f"colour '{color}' is not a hex colour; the default was used.")
            color = "#123532"
        return {"name": _text(row.get("label"))[:120], "color": color}

    _import_terms(report, rows, Badge, SEQ_BADGE, build)
    return _finish(report, rows, user=user)


# ── Entries ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _entry_status(row, report: Report) -> tuple[str, dict]:
    raw = _text(row.get("status")).lower()
    status = STATUS_MAP.get(raw)
    if status is None:
        report.violation(row["id"], "status_unknown", f"status '{raw}' is unknown; imported as DRAFT.")
        status = Entry.Status.DRAFT
    extra = {}
    if status == Entry.Status.ARCHIVED:
        extra["archived_at"] = _dt(row.get("deleted_at")) or _dt(row.get("updated_at"))
    else:
        extra["archived_at"] = None
    return status, extra


@transaction.atomic
def import_entries(rows, *, user_map: Mapping | None = None, media_map: Mapping | None = None, user=None) -> dict:
    report, rows = Report("content_entry"), list(rows)
    media = _resolver("media_asset", MediaAsset, media_map)
    reserved = {row["id"] for row in rows}
    for row in rows:
        row_id = row["id"]
        collection = mapped("catalog_collection", row.get("collection_id"), Collection)
        if collection is None:
            report.violation(row_id, "collection_unmapped", f"collection {row.get('collection_id')} was not imported.")
            report.skipped += 1
            continue
        existing = mapped(report.table, row_id, Entry)
        slug = _text(row.get("slug"))
        clash = Entry.objects.filter(collection=collection, slug=slug).exclude(pk=getattr(existing, "pk", None))
        if clash.exists():
            report.violation(row_id, "slug_clash", f"slug '{slug}' is already used in '{collection.api_uid}' by another entry; row refused.")
            report.skipped += 1
            continue
        error = slug_error(slug)
        if error:
            report.violation(row_id, "slug_invalid", f"slug '{slug}' fails today's validator ({error}); imported verbatim.")
        status, lifecycle = _entry_status(row, report)
        published_at = _dt(row.get("published_at"))
        if status == Entry.Status.PUBLISHED and published_at is None:
            published_at = _dt(row.get("published_on")) or _dt(row.get("updated_at"))
            report.violation(row_id, "published_at_missing", "published entry without published_at; the display date was used.")
        read_time = row.get("read_time")
        if read_time is not None and not 0 <= int(read_time) <= MAX_SMALLINT:
            report.violation(row_id, "read_time_out_of_range", f"read_time {read_time} dropped.")
            read_time = None
        template = mapped("catalog_template", row.get("template_id"), Template) if row.get("template_id") is not None else None
        if row.get("template_id") is not None and template is None:
            report.violation(row_id, "template_unmapped", f"template {row.get('template_id')} was not imported; entry has no template.")
        author = mapped("catalog_author", row.get("author_id"), Author) if row.get("author_id") is not None else None
        if row.get("author_id") is not None and author is None:
            report.violation(row_id, "author_unmapped", f"author {row.get('author_id')} was not imported; byline dropped.")
        values = {
            "collection": collection,
            "template": template,
            "title": _text(row.get("title"))[:255],
            "slug": slug,
            "excerpt": _text(row.get("excerpt")),
            "summary": row.get("summary") or [],
            "introduction": row.get("introduction") or [],
            "warning": _text(row.get("warning")),
            "insights": _text(row.get("insights")),
            "read_time": read_time,
            "is_featured": bool(row.get("is_featured")),
            "sort_order": row.get("sort_order"),
            "author": author,
            "cover_image": _media(media, row.get("cover_image_id"), report, row_id, "cover_image_id"),
            "status": status,
            "published_at": published_at,
            "published_on": _dt(row.get("published_on")),
            "scheduled_for": None,
            "created_by": _user(user_map, row.get("created_by_id"), report, row_id, "created_by_id"),
            "updated_by": _user(user_map, row.get("updated_by_id"), report, row_id, "updated_by_id"),
            **lifecycle,
        }
        create_only = None
        if existing is None:
            create_only = {"uid": row["document_id"], "delivery_id": _delivery_id(Entry, row_id, SEQ_ENTRY, report, row_id, reserved)}
        upsert(report, report.table, row_id, Entry, values, create_only=create_only, preserve=_stamps(row))
    _continue_sequence(Entry, SEQ_ENTRY)
    return _finish(report, rows, user=user)


def _import_links(table: str, rows, link_model: type[Model], field_name: str, term_table: str, term_model: type[Model], user=None) -> dict:
    report, rows = Report(table), list(rows)
    wanted: dict[int, set[int]] = defaultdict(set)
    for row in rows:
        entry = mapped("content_entry", row.get("entry_id"), Entry)
        term = mapped(term_table, row.get(f"{field_name}_id"), term_model)
        if entry is None or term is None:
            report.violation(row["id"], "link_unmapped", f"entry {row.get('entry_id')} or {field_name} {row.get(f'{field_name}_id')} was not imported.")
            report.skipped += 1
            continue
        wanted[entry.pk].add(term.pk)
        link, created = link_model.objects.get_or_create(entry=entry, **{field_name: term})
        remember(table, row["id"], link)
        if created:
            report.created += 1
        else:
            report.skipped += 1
    for entry_pk, term_pks in wanted.items():
        removed, _ = link_model.objects.filter(entry_id=entry_pk).exclude(**{f"{field_name}_id__in": term_pks}).delete()
        report.updated += removed
    return _finish(report, rows, user=user)


def import_entry_categories(rows, *, user=None) -> dict:
    with transaction.atomic():
        return _import_links("content_entry_categories", rows, EntryCategory, "category", "catalog_category", Category, user)


def import_entry_tags(rows, *, user=None) -> dict:
    with transaction.atomic():
        return _import_links("content_entry_tags", rows, EntryTag, "tag", "catalog_tag", Tag, user)


def import_entry_badges(rows, *, user=None) -> dict:
    with transaction.atomic():
        return _import_links("content_entry_badges", rows, EntryBadge, "badge", "catalog_badge", Badge, user)


def _entry_or_violation(report: Report, row) -> Entry | None:
    entry = mapped("content_entry", row.get("entry_id"), Entry)
    if entry is None:
        report.violation(row["id"], "entry_unmapped", f"entry {row.get('entry_id')} was not imported.")
        report.skipped += 1
    return entry


@transaction.atomic
def import_slug_history(rows, *, user=None) -> dict:
    report, rows = Report("content_entry_slug_history"), list(rows)
    for row in rows:
        entry = _entry_or_violation(report, row)
        if entry is None:
            continue
        slug, active = _text(row.get("slug")), bool(row.get("is_active", True))
        existing = mapped(report.table, row["id"], EntrySlugHistory)
        if active:
            live_clash = Entry.objects.filter(collection_id=entry.collection_id, slug=slug).exists()
            alias_clash = EntrySlugHistory.objects.filter(collection_id=entry.collection_id, slug=slug, active=True).exclude(pk=getattr(existing, "pk", None)).exists()
            if live_clash or alias_clash:
                report.violation(row["id"], "alias_clash", f"alias '{slug}' collides with a live slug or another active alias; imported inactive.")
                active = False
        values = {"entry": entry, "collection": entry.collection, "slug": slug, "active": active, "note": _text(row.get("note"))[:255]}
        created_at = _dt(row.get("created_at"))
        upsert(report, report.table, row["id"], EntrySlugHistory, values, preserve={"created_at": created_at, "updated_at": created_at} if created_at else None)
    return _finish(report, rows, user=user)


@transaction.atomic
def import_content_blocks(rows, *, user=None) -> dict:
    report, rows = Report("content_content_block"), list(rows)
    reserved = {row["id"] for row in rows}
    for row in rows:
        entry = _entry_or_violation(report, row)
        if entry is None:
            continue
        component = _text(row.get("component")) or DEFAULT_COMPONENTS[ContentBlock.Kind.RICH_TEXT]
        kind = KIND_BY_COMPONENT.get(component)
        if kind is None:
            report.violation(row["id"], "component_unknown", f"component '{component}' has no platform kind; kept verbatim as RICH_TEXT.")
            kind = ContentBlock.Kind.RICH_TEXT
        if not COMPONENT_RE.match(component):
            report.violation(row["id"], "component_malformed", f"component '{component}' is not a component uid; kept verbatim.")
        values = {"entry": entry, "kind": kind, "component": component[:120], "data": row.get("body") if row.get("body") is not None else [], "position": int(row.get("order") or 0)}
        create_only = None if mapped(report.table, row["id"], ContentBlock) else {"delivery_id": _delivery_id(ContentBlock, row["id"], SEQ_BLOCK, report, row["id"], reserved)}
        upsert(report, report.table, row["id"], ContentBlock, values, create_only=create_only)
    _continue_sequence(ContentBlock, SEQ_BLOCK)
    return _finish(report, rows, user=user)


@transaction.atomic
def import_entry_images(rows, *, media_map: Mapping | None = None, user=None) -> dict:
    report, rows = Report("content_entry_image"), list(rows)
    media = _resolver("media_asset", MediaAsset, media_map)
    for row in rows:
        entry = _entry_or_violation(report, row)
        if entry is None:
            continue
        url = _text(row.get("external_url")).strip()
        asset = None
        if url:
            if row.get("media_asset_id") is not None:
                report.violation(row["id"], "two_sources", "row had both a media asset and an external URL; the URL (what the CMS delivered) was kept.")
        else:
            asset = _media(media, row.get("media_asset_id"), report, row["id"], "media_asset_id")
            if asset is None:
                report.violation(row["id"], "image_without_source", "image row has neither a resolvable asset nor a URL; row refused.")
                report.skipped += 1
                continue
        values = {"entry": entry, "group_key": _text(row.get("group_key"))[:60], "position": int(row.get("position") or 0), "media_asset": asset, "external_url": url}
        upsert(report, report.table, row["id"], EntryImage, values)
    return _finish(report, rows, user=user)


@transaction.atomic
def import_attribute_values(rows, *, user=None) -> dict:
    report, rows = Report("content_entry_attribute_value"), list(rows)
    for row in rows:
        entry = _entry_or_violation(report, row)
        if entry is None:
            continue
        key, value = _text(row.get("slot_key"))[:60], row.get("value")
        slot = TemplateAttributeSlot.objects.filter(template_id=entry.template_id, key=key).first() if entry.template_id else None
        if slot is None:
            report.violation(row["id"], "slot_unknown", f"'{key}' is not a slot of the entry's template; value kept verbatim.")
        else:
            try:
                value = coerce_value(slot, value, lenient=True)
            except ValueError as exc:
                report.violation(row["id"], "value_invalid", f"'{key}' {exc}; value kept verbatim.")
        upsert(report, report.table, row["id"], EntryAttributeValue, {"entry": entry, "slot_key": key, "value": value})
    return _finish(report, rows, user=user)


@transaction.atomic
def import_entry_seo(rows, *, user=None) -> dict:
    report, rows = Report("content_seo"), list(rows)
    for row in rows:
        entry = _entry_or_violation(report, row)
        if entry is None:
            continue
        values = {
            "entry": entry,
            "seo_title": _text(row.get("meta_title"))[:255],
            "meta_description": _text(row.get("meta_description")),
            "canonical_url": _text(row.get("canonical_url"))[:1000],
            "keywords": _text(row.get("keywords")),
        }
        existing = EntrySeo.all_objects.filter(entry=entry).first()
        if existing is not None and mapped(report.table, row["id"], EntrySeo) is None:
            remember(report.table, row["id"], existing)  # one SEO block per entry: adopt the existing row
        upsert(report, report.table, row["id"], EntrySeo, values)
    return _finish(report, rows, user=user)


# ── Everything, in dependency order ──────────────────────────────────────────────────────────────────────────────────
TABLE_ORDER = (
    "catalog_collection",
    "catalog_template",
    "catalog_template_image_group",
    "catalog_template_attribute_slot",
    "catalog_author",
    "catalog_category",
    "catalog_tag",
    "catalog_badge",
    "content_entry",
    "content_entry_categories",
    "content_entry_tags",
    "content_entry_badges",
    "content_entry_slug_history",
    "content_content_block",
    "content_entry_image",
    "content_entry_attribute_value",
    "content_seo",
)


def import_all(
    tables: Mapping[str, list], *, user_map: Mapping | None = None, media_map: Mapping | None = None, path_prefixes: Mapping | None = None, user=None, dry_run: bool = False
) -> dict[str, dict]:
    """Import every CMS blog table present in ``tables``; ``dry_run`` reports without writing (rolled back)."""
    importers = {
        "catalog_collection": lambda rows: import_collections(rows, path_prefixes=path_prefixes, user=user),
        "catalog_template": lambda rows: import_templates(rows, user=user),
        "catalog_template_image_group": lambda rows: import_template_image_groups(rows, user=user),
        "catalog_template_attribute_slot": lambda rows: import_template_attribute_slots(rows, user=user),
        "catalog_author": lambda rows: import_authors(rows, user=user),
        "catalog_category": lambda rows: import_categories(rows, user=user),
        "catalog_tag": lambda rows: import_tags(rows, user=user),
        "catalog_badge": lambda rows: import_badges(rows, user=user),
        "content_entry": lambda rows: import_entries(rows, user_map=user_map, media_map=media_map, user=user),
        "content_entry_categories": lambda rows: import_entry_categories(rows, user=user),
        "content_entry_tags": lambda rows: import_entry_tags(rows, user=user),
        "content_entry_badges": lambda rows: import_entry_badges(rows, user=user),
        "content_entry_slug_history": lambda rows: import_slug_history(rows, user=user),
        "content_content_block": lambda rows: import_content_blocks(rows, user=user),
        "content_entry_image": lambda rows: import_entry_images(rows, media_map=media_map, user=user),
        "content_entry_attribute_value": lambda rows: import_attribute_values(rows, user=user),
        "content_seo": lambda rows: import_entry_seo(rows, user=user),
    }
    results: dict[str, dict] = {}
    with transaction.atomic():
        for table in TABLE_ORDER:
            if table in tables:
                results[table] = importers[table](tables[table])
        if dry_run:
            transaction.set_rollback(True)
    return results
