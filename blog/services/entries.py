"""Entry authoring (staff ``content/entries/``): create, edit, delete and the child collections.

* One call saves everything: scalar fields, taxonomy links (uids on write), content blocks, images, attribute values
  and the SEO block. **Child collections are replace-all**: sending ``content_blocks`` / ``images`` /
  ``attribute_values`` / ``category_uids`` … replaces the whole set; omitting the key leaves it untouched.
* Drafts save loose (required slots and image counts are enforced at publish), but every value is validated against
  its template: unknown image groups or slots, ill-typed values and non-public media are refused (400).
* The slug is validated on every write path (legacy weakness §17 #10) and must be free in the collection, aliases
  included (409 ``slug_taken``). A rename keeps the old slug as an alias; the collection cannot change.
* Every write is versioned (``expected_version`` → 409 ``stale_version``), audited and bumps ``blog:entries``; an
  edit of a published entry emits ``blog.entry_updated`` / ``blog.entry_slug_changed`` for revalidation.
* ``DELETE`` is a soft delete (``archive`` permission): the slug is freed and the entry's aliases are retired.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch
from django.utils import timezone

from audit.services import changes, record, snapshot
from blog.models import (
    Category,
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
from blog.models.taxonomy import Badge
from blog.services.attributes import coerce_value
from blog.services.common import MAX_REVALIDATE_PATHS, NS_ENTRIES, SEQ_BLOCK, SEQ_ENTRY, invalid, next_delivery_id
from blog.services.slugs import deactivate_entry_aliases, ensure_slug_usable, record_slug_change, slug_conflict
from blog.validators import validate_component, validate_key
from core.errors import DomainError
from core.models import actor_or_none
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from media.models import MediaAsset

SCALAR_FIELDS = (
    "title",
    "slug",
    "excerpt",
    "summary",
    "introduction",
    "warning",
    "insights",
    "read_time",
    "is_featured",
    "sort_order",
    "locale",
    "published_on",
    "template",
    "author",
    "cover_image",
)
SNAPSHOT_FIELDS = ("collection", "template", "title", "slug", "status", "excerpt", "warning", "insights", "read_time", "is_featured", "sort_order", "locale", "author", "cover_image", "published_on")
LARGE_FIELDS = ("summary", "introduction")
SEO_FIELDS = ("seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex", "keywords")
LINKS = {"categories": (EntryCategory, "category"), "tags": (EntryTag, "tag"), "badges": (EntryBadge, "badge")}
DEFAULT_COMPONENTS = {
    ContentBlock.Kind.RICH_TEXT: "shared.rich-text",
    ContentBlock.Kind.IMAGE: "shared.media",
    ContentBlock.Kind.QUOTE: "shared.quote",
    ContentBlock.Kind.CTA: "shared.cta",
    ContentBlock.Kind.EMBED: "shared.embed",
    ContentBlock.Kind.TABLE: "shared.table",
}
KIND_BY_COMPONENT = {component: kind for kind, component in DEFAULT_COMPONENTS.items()}
LIST_KINDS = (ContentBlock.Kind.RICH_TEXT, ContentBlock.Kind.TABLE)


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def list_queryset():
    """Slim rows for the staff list (the cover thumbnail falls back to the first image)."""
    return Entry.objects.select_related("collection", "template", "author", "cover_image").prefetch_related(
        Prefetch("images", queryset=EntryImage.objects.select_related("media_asset").order_by("group_key", "position", "id"))
    )


def detail_queryset():
    return Entry.objects.select_related("collection", "template", "author", "author__avatar", "cover_image", "verified_by", "seo", "seo__og_image").prefetch_related(
        Prefetch("categories", queryset=Category.objects.order_by("name", "id")),
        Prefetch("tags", queryset=Tag.objects.order_by("name", "id")),
        Prefetch("badges", queryset=Badge.objects.order_by("name", "id")),
        Prefetch("content_blocks", queryset=ContentBlock.objects.order_by("position", "delivery_id")),
        Prefetch("images", queryset=EntryImage.objects.select_related("media_asset").order_by("group_key", "position", "id")),
        Prefetch("attribute_values", queryset=EntryAttributeValue.objects.order_by("slot_key", "id")),
        Prefetch("slug_history", queryset=EntrySlugHistory.objects.order_by("-created_at", "-id")),
    )


def live_seo(entry: Entry) -> EntrySeo | None:
    try:
        seo = entry.seo
    except EntrySeo.DoesNotExist:
        return None
    return seo if seo.deleted_at is None else None


def entry_snapshot(entry: Entry) -> dict:
    data = snapshot(entry, SNAPSHOT_FIELDS)
    for name in LINKS:
        data[name] = sorted(str(uid) for uid in getattr(entry, name).values_list("uid", flat=True))
    return data


# ── Validation ───────────────────────────────────────────────────────────────────────────────────────────────────────
def check_public_image(asset: MediaAsset | None, field: str) -> None:
    if asset is None:
        return
    if asset.deleted_at is not None or asset.kind not in (MediaAsset.Kind.IMAGE, MediaAsset.Kind.PHOTO) or not asset.is_public:
        raise invalid(field, "Must be a public image from the media library.", "invalid_media")


def _groups(template: Template | None) -> dict[str, TemplateImageGroup] | None:
    return None if template is None else {group.key: group for group in template.image_groups.all()}


def _slots(template: Template | None) -> dict[str, TemplateAttributeSlot]:
    return {} if template is None else {slot.key: slot for slot in template.attribute_slots.all()}


def validate_images(images: list[dict], template: Template | None) -> list[dict]:
    """Each image row: a known group of the template (any key without a template), exactly one source, public media."""
    groups = _groups(template)
    errors, rows = [], []
    for index, image in enumerate(images):
        key = image.get("group_key") or ""
        try:
            validate_key(key)
        except ValidationError:
            errors.append(f"images[{index}]: '{key}' is not a valid group key.")
            continue
        if groups is not None and key not in groups:
            errors.append(f"images[{index}]: '{key}' is not an image group of template '{template.slug}'.")
        asset, url = image.get("media_asset"), (image.get("external_url") or "").strip()
        if (asset is None) == (not url):
            errors.append(f"images[{index}]: give exactly one of media_asset_uid or external_url.")
        elif asset is not None and (asset.deleted_at is not None or not asset.is_public or asset.kind not in (MediaAsset.Kind.IMAGE, MediaAsset.Kind.PHOTO)):
            errors.append(f"images[{index}]: the media asset must be a public image.")
        rows.append({"group_key": key, "position": image.get("position", index), "media_asset": asset, "external_url": url, "alt": image.get("alt") or ""})
    if errors:
        raise DomainError("validation_error", errors[0], errors={"images": errors})
    return rows


def validate_attributes(values: list[dict], template: Template | None, *, lenient: bool = False) -> list[dict]:
    """Each value: a slot of the template, at most once, typed per the slot."""
    if values and template is None:
        raise invalid("attribute_values", "Attribute values need a template that defines the slots.", "template_required")
    slots = _slots(template)
    errors, rows, seen = [], [], set()
    for index, item in enumerate(values):
        key = item.get("slot_key") or ""
        if key in seen:
            errors.append(f"attribute_values[{index}]: '{key}' is given twice.")
            continue
        seen.add(key)
        slot = slots.get(key)
        if slot is None:
            errors.append(f"attribute_values[{index}]: '{key}' is not an attribute slot of template '{template.slug}'.")
            continue
        try:
            rows.append({"slot_key": key, "value": coerce_value(slot, item.get("value"), lenient=lenient)})
        except ValueError as exc:
            errors.append(f"{slot.label} ({key}) {exc}.")
    if errors:
        raise DomainError("validation_error", errors[0], errors={"attribute_values": errors})
    return rows


def validate_blocks(blocks: list[dict]) -> list[dict]:
    errors, rows = [], []
    for index, block in enumerate(blocks):
        component = block.get("component")
        kind = block.get("kind") or KIND_BY_COMPONENT.get(component or "", ContentBlock.Kind.RICH_TEXT)
        component = component or DEFAULT_COMPONENTS[kind]
        try:
            validate_component(component)
        except ValidationError:
            errors.append(f"content_blocks[{index}]: '{component}' is not a component uid (e.g. shared.rich-text).")
        data = block.get("data", [])
        if kind in LIST_KINDS and not (isinstance(data, list) and all(isinstance(item, dict) for item in data)):
            errors.append(f"content_blocks[{index}]: a {kind} block's data is a list of blocks.")
        elif not isinstance(data, (list, dict)):
            errors.append(f"content_blocks[{index}]: data must be an object or a list.")
        rows.append({"kind": kind, "component": component, "data": data, "position": block.get("position", index)})
    if errors:
        raise DomainError("validation_error", errors[0], errors={"content_blocks": errors})
    return rows


def _check_template_fit(entry: Entry, template: Template | None, data: dict) -> None:
    """A template change must still fit the rows the client is not replacing."""
    if "images" not in data and template is not None:
        groups = _groups(template)
        stray = sorted({image.group_key for image in entry.images.all()} - set(groups))
        if stray:
            raise invalid("images", f"Image groups {', '.join(stray)} do not exist in template '{template.slug}'; send the images for the new template.", "template_mismatch")
    if "attribute_values" not in data:
        existing = list(entry.attribute_values.all())
        if existing and template is None:
            raise invalid("attribute_values", "Removing the template needs the attribute values cleared (send attribute_values: []).", "template_mismatch")
        slots = _slots(template)
        stray = sorted({value.slot_key for value in existing} - set(slots))
        if stray:
            raise invalid("attribute_values", f"Slots {', '.join(stray)} do not exist in template '{template.slug}'; send the attribute values for the new template.", "template_mismatch")


def _check_seo(seo: dict | None) -> None:
    if seo:
        check_public_image(seo.get("og_image"), "seo.og_image_uid")
        if "schema_extra" in seo and not isinstance(seo["schema_extra"], dict):
            raise invalid("seo.schema_extra", "Must be an object.")


# ── Child writers (inside the caller's transaction) ─────────────────────────────────────────────────────────────────
def _retire(queryset, user) -> None:
    actor = actor_or_none(user)
    now = timezone.now()
    queryset.update(deleted_at=now, updated_at=now, updated_by=actor, version=F("version") + 1)


def _stamped(rows, user):
    for row in rows:
        stamp_create(row, user)
    return rows


def write_links(entry: Entry, name: str, terms: list) -> None:
    link_model, field = LINKS[name]
    link_model.objects.filter(entry=entry).delete()  # link rows carry no history (DV-17); the audit row does
    link_model.objects.bulk_create([link_model(entry=entry, **{field: term}) for term in dict.fromkeys(terms)])


def write_blocks(entry: Entry, blocks: list[dict], user, *, delivery_ids: list[int] | None = None) -> None:
    _retire(ContentBlock.objects.filter(entry=entry), user)
    ids = delivery_ids or [next_delivery_id(SEQ_BLOCK) for _ in blocks]
    ContentBlock.objects.bulk_create(_stamped([ContentBlock(entry=entry, delivery_id=ids[index], **block) for index, block in enumerate(blocks)], user))


def write_images(entry: Entry, images: list[dict], user) -> None:
    _retire(EntryImage.objects.filter(entry=entry), user)
    EntryImage.objects.bulk_create(_stamped([EntryImage(entry=entry, **image) for image in images], user))


def write_attributes(entry: Entry, values: list[dict], user) -> None:
    _retire(EntryAttributeValue.objects.filter(entry=entry), user)
    EntryAttributeValue.objects.bulk_create(_stamped([EntryAttributeValue(entry=entry, **value) for value in values], user))


def write_seo(entry: Entry, seo: dict | None, user) -> None:
    """Upsert the SEO block (``None`` removes it). The one row per entry is restored rather than re-created."""
    row = EntrySeo.all_objects.filter(entry=entry).first()
    if seo is None:
        if row is not None and row.deleted_at is None:
            row.soft_delete(user)
        return
    if row is None:
        row = EntrySeo(entry=entry, **{name: seo[name] for name in SEO_FIELDS if name in seo})
        stamp_create(row, user)
        row.save()
        return
    values = {name: seo[name] for name in SEO_FIELDS if name in seo and seo[name] != getattr(row, name)}
    if row.deleted_at is not None:
        values["deleted_at"] = None
    if values:
        row.versioned_update(user, **values)


# ── Writes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _prepare_children(data: dict, template: Template | None) -> dict:
    prepared = {}
    if "content_blocks" in data:
        prepared["content_blocks"] = validate_blocks(data["content_blocks"] or [])
    if "images" in data:
        prepared["images"] = validate_images(data["images"] or [], template)
    if "attribute_values" in data:
        prepared["attribute_values"] = validate_attributes(data["attribute_values"] or [], template)
    if "seo" in data:
        _check_seo(data["seo"])
    return prepared


def _write_children(entry: Entry, data: dict, prepared: dict, user) -> list[str]:
    written = []
    for name in LINKS:
        if name in data:
            write_links(entry, name, data[name] or [])
            written.append(name)
    for name, writer in (("content_blocks", write_blocks), ("images", write_images), ("attribute_values", write_attributes)):
        if name in prepared:
            writer(entry, prepared[name], user)
            written.append(name)
    if "seo" in data:
        write_seo(entry, data["seo"], user)
        written.append("seo")
    return written


@transaction.atomic
def create_entry(*, user, data) -> Entry:
    collection = data["collection"]
    template = data.get("template")
    ensure_slug_usable(collection, data.get("slug", ""))
    check_public_image(data.get("cover_image"), "cover_image_uid")
    prepared = _prepare_children(data, template)
    entry = Entry(delivery_id=next_delivery_id(SEQ_ENTRY), collection=collection, status=Entry.Status.DRAFT, **{name: data[name] for name in SCALAR_FIELDS if name in data})
    stamp_create(entry, user)
    try:
        with transaction.atomic():
            entry.save()
    except IntegrityError:
        raise slug_conflict() from None
    written = _write_children(entry, data, prepared, user)
    record("blog.entry_created", obj=entry, actor=user, after={**entry_snapshot(entry), "children": written})
    bump(NS_ENTRIES)
    return entry


def active_alias_slugs(entry: Entry) -> list[str]:
    return list(EntrySlugHistory.objects.filter(entry=entry, active=True).order_by("created_at", "id").values_list("slug", flat=True)[:MAX_REVALIDATE_PATHS])


def publication_event(entry: Entry, event_type: str, *, old_slugs=(), aliases=None, **extra) -> None:
    """Emit a blog lifecycle event with the paths to revalidate (deduplicated per entry version).

    The entry's active alias URLs are revalidated too: the website renders the article itself under an old URL (the
    delivery resolves the alias), so an edit — or an unpublished article — must not linger there until the ISR window
    expires. ``aliases`` overrides the lookup (a deleted entry's aliases are retired before the event is emitted).
    """
    prefix = entry.collection.path_prefix
    if aliases is None:
        aliases = active_alias_slugs(entry)
    paths = list(dict.fromkeys([prefix, *(f"{prefix}/{slug}" for slug in (entry.slug, *old_slugs, *aliases))]))[: MAX_REVALIDATE_PATHS + 1]
    payload = {"entry_uid": str(entry.uid), "collection": entry.collection.api_uid, "slug": entry.slug, "status": entry.status, "paths": paths, **extra}
    emit(event_type, payload, aggregate_type="blog.entry", aggregate_uid=entry.uid, dedup_key=f"{event_type}:{entry.uid}:{entry.version}")


@transaction.atomic
def update_entry(instance: Entry, *, user, data, expected_version=None) -> Entry:
    entry = Entry.objects.select_for_update(of=("self",)).select_related("collection", "template").get(pk=instance.pk)
    check_version(entry, expected_version)
    if "collection" in data and data["collection"] is not None and data["collection"].pk != entry.collection_id:
        raise invalid("collection_uid", "An entry cannot move to another collection; duplicate it instead.", "collection_immutable")
    values = {name: data[name] for name in SCALAR_FIELDS if name in data and data[name] != getattr(entry, name)}
    old_slug = entry.slug
    if "slug" in values:
        ensure_slug_usable(entry.collection, values["slug"], exclude_entry=entry)
    if "cover_image" in values:
        check_public_image(values["cover_image"], "cover_image_uid")
    template = values["template"] if "template" in values else entry.template
    if "template" in values:
        _check_template_fit(entry, template, data)
    prepared = _prepare_children(data, template)
    child_keys = [name for name in (*LINKS, "content_blocks", "images", "attribute_values", "seo") if name in data]
    if not values and not child_keys:
        return entry
    before = entry_snapshot(entry)
    try:
        with transaction.atomic():
            entry.versioned_update(user, **values)
    except IntegrityError:
        raise slug_conflict() from None
    if "slug" in values:
        record_slug_change(entry, old_slug, user=user)
    written = _write_children(entry, data, prepared, user)
    changed_before, changed_after = changes(before, entry_snapshot(entry))
    large = [name for name in LARGE_FIELDS if name in values]
    record("blog.entry_updated", obj=entry, actor=user, before=changed_before, after={**changed_after, "changed": large + written})
    bump(NS_ENTRIES)
    if entry.status == Entry.Status.PUBLISHED:
        if "slug" in values:
            publication_event(entry, "blog.entry_slug_changed", old_slugs=[old_slug], old_slug=old_slug)
        else:
            publication_event(entry, "blog.entry_updated")
    return entry


@transaction.atomic
def delete_entry(instance: Entry, *, user, expected_version=None) -> None:
    entry = Entry.objects.select_for_update(of=("self",)).select_related("collection").get(pk=instance.pk)
    check_version(entry, expected_version)
    was_public = entry.status == Entry.Status.PUBLISHED
    before = entry_snapshot(entry)
    aliases = active_alias_slugs(entry)
    entry.soft_delete(user)
    deactivate_entry_aliases(entry, user=user)
    record("blog.entry_deleted", obj=entry, actor=user, before=before)
    bump(NS_ENTRIES)
    if was_public:
        publication_event(entry, "blog.entry_deleted", aliases=aliases)
