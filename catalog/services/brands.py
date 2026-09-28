"""``catalog/brands/`` — manufacturers.

* the name is unique case-insensitively among live brands; the slug is derived from the name unless given;
* renaming a brand re-labels its components whose printed ``brand_label`` was the old name (history recorded);
* a brand with live components cannot be deleted (409 ``brand_in_use``).
"""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from catalog.models import Brand, Component
from catalog.services.common import CACHE_NAMESPACE, IMAGE_RULE, check_assets, unique_slug, validation_error
from catalog.services.history import record_change
from core.errors import Conflict
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

SNAPSHOT_FIELDS = ("name", "slug", "country", "website", "logo", "is_active")
EDITABLE_FIELDS = ("name", "slug", "country", "website", "logo", "is_active")
_SPACES = re.compile(r"\s+")


def normalise_name(name: str | None) -> str:
    """Display form of a brand name: trimmed, inner whitespace collapsed (``"kanberry "`` → ``"kanberry"``)."""
    return _SPACES.sub(" ", (name or "").strip())


def name_key(name: str | None) -> str:
    """Matching key: the normalised name, case-folded (``"RenewSys"`` and ``"Renewsys"`` are one brand)."""
    return normalise_name(name).casefold()


def brands_queryset():
    return Brand.objects.select_related("logo").order_by("name", "id")


def find_by_name(name: str) -> Brand | None:
    key = name_key(name)
    if not key:
        return None
    return Brand.objects.filter(name__iexact=normalise_name(name)).first()


def _slug_taken(slug: str, exclude_pk=None) -> bool:
    return Brand.objects.filter(slug=slug).exclude(pk=exclude_pk).exists()


def _integrity_conflict(exc: IntegrityError) -> Conflict:
    if "slug" in str(exc):
        return Conflict("brand_slug_taken", "Another brand already uses this slug.", errors={"slug": ["Already in use."]})
    return Conflict("brand_name_taken", "Another brand already has this name.", errors={"name": ["Already in use."]})


@transaction.atomic
def create_brand(*, user, data) -> Brand:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    values["name"] = normalise_name(values.get("name"))
    if not values["name"]:
        raise validation_error({"name": ["This field may not be blank."]})
    check_assets(values, {"logo": IMAGE_RULE})
    if not values.get("slug"):
        values["slug"] = unique_slug(values["name"], _slug_taken, max_length=120, fallback="brand")
    brand = Brand(**values)
    stamp_create(brand, user)
    try:
        with transaction.atomic():
            brand.save()
    except IntegrityError as exc:
        raise _integrity_conflict(exc) from None
    record("catalog.brand_created", obj=brand, actor=user, after=snapshot(brand, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)
    return brand


def _relabel_components(brand: Brand, old_name: str, *, user) -> int:
    count = 0
    for component in Component.objects.select_for_update().filter(brand=brand, brand_label=old_name):
        component.versioned_update(user, brand_label=brand.name)
        record_change(component, user=user, field="brand_label", old=old_name, new=brand.name, reason="brand renamed")
        count += 1
    return count


@transaction.atomic
def update_brand(instance: Brand, *, user, data, expected_version=None) -> Brand:
    brand = Brand.objects.select_for_update().get(pk=instance.pk)
    check_version(brand, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    if "name" in values:
        values["name"] = normalise_name(values["name"])
        if not values["name"]:
            raise validation_error({"name": ["This field may not be blank."]})
    check_assets(values, {"logo": IMAGE_RULE})
    values = {name: value for name, value in values.items() if getattr(brand, name) != value}
    if not values:
        return brand
    before = snapshot(brand, SNAPSHOT_FIELDS)
    old_name = brand.name
    try:
        with transaction.atomic():
            brand.versioned_update(user, **values)
    except IntegrityError as exc:
        raise _integrity_conflict(exc) from None
    relabelled = _relabel_components(brand, old_name, user=user) if "name" in values else 0
    changed_before, changed_after = changes(before, snapshot(brand, SNAPSHOT_FIELDS))
    record("catalog.brand_updated", obj=brand, actor=user, before=changed_before, after=changed_after, note=f"{relabelled} components relabelled" if relabelled else "")
    bump(CACHE_NAMESPACE)
    if relabelled:
        emit("catalog.brand_renamed", {"brand_uid": str(brand.uid), "old_name": old_name, "name": brand.name}, aggregate_type="catalog.brand", aggregate_uid=brand.uid)
    return brand


@transaction.atomic
def delete_brand(instance: Brand, *, user, expected_version=None) -> None:
    brand = Brand.objects.select_for_update().get(pk=instance.pk)
    check_version(brand, expected_version)
    in_use = Component.objects.filter(brand=brand).count()
    if in_use:
        raise Conflict("brand_in_use", f"{in_use} live components use this brand; move or delete them first.", errors={"components": [str(in_use)]})
    brand.soft_delete(user)
    record("catalog.brand_deleted", obj=brand, actor=user, before=snapshot(brand, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)


def ensure_brand(name: str | None, *, user) -> tuple[Brand | None, bool]:
    """The live brand called ``name`` (case-insensitive), created when missing; ``(None, False)`` for a blank name.

    Used by importers; returns ``(brand, created)``.
    """
    display = normalise_name(name)
    if not display:
        return None, False
    brand = find_by_name(display)
    if brand is not None:
        return brand, False
    return create_brand(user=user, data={"name": display}), True
