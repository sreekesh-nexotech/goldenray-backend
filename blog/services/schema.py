"""Collections, templates, template image groups and attribute slots (staff ``content/collections/``, ``content/templates/``).

* ``api_uid``, template ``slug`` and group/slot ``key`` are validated here (field validators never run on ``save()``);
* group/slot keys are **immutable** (they are the delivery contract); labels are free to change;
* a collection or template used by a live entry cannot be deleted (409 ``collection_in_use`` / ``template_in_use``);
* :func:`duplicate_template` copies a template with its groups and slots in one transaction (legacy weakness §17 #9);
* every write is audited, bumps the delivery cache namespace and, when published pages change, emits
  ``blog.content_changed`` for revalidation.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q

from audit.services import changes, record, snapshot
from blog.models import Collection, Entry, Template, TemplateAttributeSlot, TemplateImageGroup
from blog.services.attributes import validate_slot_options
from blog.services.common import NS_COLLECTIONS, NS_TEMPLATES, django_errors, emit_content_changed, in_use, invalid, validate_simple_slug
from blog.validators import validate_api_uid, validate_key, validate_path_prefix
from core.errors import Conflict
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

COLLECTION_FIELDS = ("api_uid", "singular_name", "plural_name", "description", "path_prefix", "is_active")
TEMPLATE_FIELDS = ("slug", "name", "description", "is_active", "sort_order")
GROUP_FIELDS = ("key", "label", "repeatable", "max_items", "required", "position")
SLOT_FIELDS = ("key", "label", "type", "options", "required", "position")
LIVE_ENTRIES = Q(entries__deleted_at__isnull=True)


# ── Collections ──────────────────────────────────────────────────────────────────────────────────────────────────────
def collections_queryset():
    return Collection.objects.annotate(entry_count=Count("entries", filter=LIVE_ENTRIES)).order_by("plural_name", "id")


def _validate_collection(data: dict) -> None:
    for field, validator in (("api_uid", validate_api_uid), ("path_prefix", validate_path_prefix)):
        if field in data:
            try:
                validator(data[field])
            except ValidationError as exc:
                raise django_errors(exc, field) from None


def _api_uid_taken() -> Conflict:
    return Conflict("api_uid_taken", "Another collection already uses this route.", errors={"api_uid": ["Already in use."]})


@transaction.atomic
def create_collection(*, user, data) -> Collection:
    _validate_collection(data)
    collection = Collection(**{name: data[name] for name in COLLECTION_FIELDS if name in data})
    stamp_create(collection, user)
    try:
        with transaction.atomic():
            collection.save()
    except IntegrityError:
        raise _api_uid_taken() from None
    record("blog.collection_created", obj=collection, actor=user, after=snapshot(collection, COLLECTION_FIELDS))
    bump(NS_COLLECTIONS)
    return collection


@transaction.atomic
def update_collection(instance: Collection, *, user, data, expected_version=None) -> Collection:
    collection = Collection.objects.select_for_update().get(pk=instance.pk)
    check_version(collection, expected_version)
    _validate_collection(data)
    before = snapshot(collection, COLLECTION_FIELDS)
    values = {name: data[name] for name in COLLECTION_FIELDS if name in data and data[name] != getattr(collection, name)}
    if not values:
        return collection
    old_prefix = collection.path_prefix
    try:
        with transaction.atomic():
            collection.versioned_update(user, **values)
    except IntegrityError:
        raise _api_uid_taken() from None
    changed_before, changed_after = changes(before, snapshot(collection, COLLECTION_FIELDS))
    record("blog.collection_updated", obj=collection, actor=user, before=changed_before, after=changed_after)
    bump(NS_COLLECTIONS)
    if {"api_uid", "path_prefix", "is_active"} & set(values):
        emit_content_changed(Entry.objects.filter(collection=collection), reason="collection_updated", extra_paths=[old_prefix, collection.path_prefix])
    return collection


@transaction.atomic
def delete_collection(instance: Collection, *, user, expected_version=None) -> None:
    collection = Collection.objects.select_for_update().get(pk=instance.pk)
    check_version(collection, expected_version)
    count = Entry.objects.filter(collection=collection).count()
    if count:
        raise in_use("collection_in_use", "collection", count)
    collection.soft_delete(user)
    record("blog.collection_deleted", obj=collection, actor=user, before=snapshot(collection, COLLECTION_FIELDS))
    bump(NS_COLLECTIONS)


# ── Templates ────────────────────────────────────────────────────────────────────────────────────────────────────────
def templates_queryset():
    return (
        Template.objects.annotate(entry_count=Count("entries", filter=LIVE_ENTRIES))
        .prefetch_related(
            Prefetch("image_groups", queryset=TemplateImageGroup.objects.order_by("position", "id")),
            Prefetch("attribute_slots", queryset=TemplateAttributeSlot.objects.order_by("position", "id")),
        )
        .order_by("sort_order", "name", "id")
    )


def _slug_taken() -> Conflict:
    return Conflict("slug_taken", "Another template already uses this slug.", errors={"slug": ["Already in use."]})


@transaction.atomic
def create_template(*, user, data) -> Template:
    validate_simple_slug(data.get("slug", ""))
    template = Template(**{name: data[name] for name in TEMPLATE_FIELDS if name in data})
    stamp_create(template, user)
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError:
        raise _slug_taken() from None
    record("blog.template_created", obj=template, actor=user, after=snapshot(template, TEMPLATE_FIELDS))
    bump(NS_TEMPLATES)
    return template


@transaction.atomic
def update_template(instance: Template, *, user, data, expected_version=None) -> Template:
    template = Template.objects.select_for_update().get(pk=instance.pk)
    check_version(template, expected_version)
    if "slug" in data:
        validate_simple_slug(data["slug"])
    before = snapshot(template, TEMPLATE_FIELDS)
    values = {name: data[name] for name in TEMPLATE_FIELDS if name in data and data[name] != getattr(template, name)}
    if not values:
        return template
    try:
        with transaction.atomic():
            template.versioned_update(user, **values)
    except IntegrityError:
        raise _slug_taken() from None
    changed_before, changed_after = changes(before, snapshot(template, TEMPLATE_FIELDS))
    record("blog.template_updated", obj=template, actor=user, before=changed_before, after=changed_after)
    bump(NS_TEMPLATES)
    return template


@transaction.atomic
def delete_template(instance: Template, *, user, expected_version=None) -> None:
    template = Template.objects.select_for_update().get(pk=instance.pk)
    check_version(template, expected_version)
    count = Entry.objects.filter(template=template).count()
    if count:
        raise in_use("template_in_use", "template", count)
    template.soft_delete(user)
    record("blog.template_deleted", obj=template, actor=user, before=snapshot(template, TEMPLATE_FIELDS))
    bump(NS_TEMPLATES)


@transaction.atomic
def duplicate_template(instance: Template, *, user, slug: str, name: str | None = None) -> Template:
    """Deep copy (groups and slots) under a new slug; existing entries stay on the original. All or nothing."""
    source = Template.objects.get(pk=instance.pk)
    copy = create_template(user=user, data={"slug": slug, "name": name or f"{source.name} (copy)", "description": source.description, "is_active": source.is_active, "sort_order": source.sort_order})
    groups = [TemplateImageGroup(template=copy, **{field: getattr(group, field) for field in GROUP_FIELDS}) for group in source.image_groups.all()]
    slots = [TemplateAttributeSlot(template=copy, **{field: getattr(slot, field) for field in SLOT_FIELDS}) for slot in source.attribute_slots.all()]
    for row in (*groups, *slots):
        stamp_create(row, user)
    TemplateImageGroup.objects.bulk_create(groups)
    TemplateAttributeSlot.objects.bulk_create(slots)
    record("blog.template_duplicated", obj=copy, actor=user, after={"source": str(source.uid), "image_groups": len(groups), "attribute_slots": len(slots)})
    return copy


# ── Image groups ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _validate_group(values: dict, current: TemplateImageGroup | None = None) -> None:
    if "key" in values:
        try:
            validate_key(values["key"])
        except ValidationError as exc:
            raise django_errors(exc, "key") from None
    repeatable = values.get("repeatable", current.repeatable if current else False)
    max_items = values.get("max_items", current.max_items if current else None)
    if max_items is not None and not repeatable:
        raise invalid("max_items", "Only a repeatable group can have max_items.")
    if max_items is not None and max_items < 1:
        raise invalid("max_items", "max_items must be at least 1 (or null for unbounded).")


def _key_taken(what: str) -> Conflict:
    return Conflict("key_taken", f"This template already has an {what} with this key.", errors={"key": ["Already in use in this template."]})


def _template_entries(template: Template):
    return Entry.objects.filter(template=template)


@transaction.atomic
def create_image_group(template: Template, *, user, data) -> TemplateImageGroup:
    _validate_group(data)
    group = TemplateImageGroup(template=template, **{name: data[name] for name in GROUP_FIELDS if name in data})
    stamp_create(group, user)
    try:
        with transaction.atomic():
            group.save()
    except IntegrityError:
        raise _key_taken("image group") from None
    record("blog.template_image_group_created", obj=group, actor=user, after={"template": str(template.uid), **snapshot(group, GROUP_FIELDS)})
    bump(NS_TEMPLATES)
    emit_content_changed(_template_entries(template), reason="template_changed")
    return group


@transaction.atomic
def update_image_group(instance: TemplateImageGroup, *, user, data, expected_version=None) -> TemplateImageGroup:
    group = TemplateImageGroup.objects.select_for_update().get(pk=instance.pk)
    check_version(group, expected_version)
    if "key" in data and data["key"] != group.key:
        raise invalid("key", "A group key is the delivery contract and cannot change; create a new group instead.", "key_immutable")
    values = {name: data[name] for name in GROUP_FIELDS if name in data and name != "key" and data[name] != getattr(group, name)}
    _validate_group(values, group)
    if not values:
        return group
    before = snapshot(group, GROUP_FIELDS)
    group.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(group, GROUP_FIELDS))
    record("blog.template_image_group_updated", obj=group, actor=user, before=changed_before, after=changed_after)
    bump(NS_TEMPLATES)
    if "repeatable" in values:
        emit_content_changed(_template_entries(group.template), reason="template_changed")
    return group


@transaction.atomic
def delete_image_group(instance: TemplateImageGroup, *, user, expected_version=None) -> None:
    group = TemplateImageGroup.objects.select_for_update().get(pk=instance.pk)
    check_version(group, expected_version)
    group.soft_delete(user)
    record("blog.template_image_group_deleted", obj=group, actor=user, before=snapshot(group, GROUP_FIELDS))
    bump(NS_TEMPLATES)
    emit_content_changed(_template_entries(group.template), reason="template_changed")


# ── Attribute slots ──────────────────────────────────────────────────────────────────────────────────────────────────
def _validate_slot(values: dict, current: TemplateAttributeSlot | None = None) -> dict:
    if "key" in values:
        try:
            validate_key(values["key"])
        except ValidationError as exc:
            raise django_errors(exc, "key") from None
    slot_type = values.get("type", current.type if current else TemplateAttributeSlot.Type.TEXT)
    options = values.get("options", current.options if current else {})
    if "type" in values or "options" in values:
        values["options"] = validate_slot_options(slot_type, options)
    return values


@transaction.atomic
def create_attribute_slot(template: Template, *, user, data) -> TemplateAttributeSlot:
    values = _validate_slot({name: data[name] for name in SLOT_FIELDS if name in data})
    if "options" not in values:
        values["options"] = validate_slot_options(values.get("type", TemplateAttributeSlot.Type.TEXT), {})
    slot = TemplateAttributeSlot(template=template, **values)
    stamp_create(slot, user)
    try:
        with transaction.atomic():
            slot.save()
    except IntegrityError:
        raise _key_taken("attribute slot") from None
    record("blog.template_attribute_slot_created", obj=slot, actor=user, after={"template": str(template.uid), **snapshot(slot, SLOT_FIELDS)})
    bump(NS_TEMPLATES)
    return slot


@transaction.atomic
def update_attribute_slot(instance: TemplateAttributeSlot, *, user, data, expected_version=None) -> TemplateAttributeSlot:
    slot = TemplateAttributeSlot.objects.select_for_update().get(pk=instance.pk)
    check_version(slot, expected_version)
    if "key" in data and data["key"] != slot.key:
        raise invalid("key", "A slot key is the delivery contract and cannot change; create a new slot instead.", "key_immutable")
    values = {name: data[name] for name in SLOT_FIELDS if name in data and name != "key" and data[name] != getattr(slot, name)}
    values = _validate_slot(values, slot)
    values = {name: value for name, value in values.items() if value != getattr(slot, name)}
    if not values:
        return slot
    before = snapshot(slot, SLOT_FIELDS)
    slot.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(slot, SLOT_FIELDS))
    record("blog.template_attribute_slot_updated", obj=slot, actor=user, before=changed_before, after=changed_after)
    bump(NS_TEMPLATES)
    return slot


@transaction.atomic
def delete_attribute_slot(instance: TemplateAttributeSlot, *, user, expected_version=None) -> None:
    slot = TemplateAttributeSlot.objects.select_for_update().get(pk=instance.pk)
    check_version(slot, expected_version)
    slot.soft_delete(user)
    record("blog.template_attribute_slot_deleted", obj=slot, actor=user, before=snapshot(slot, SLOT_FIELDS))
    bump(NS_TEMPLATES)
