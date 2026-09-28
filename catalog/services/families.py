"""``catalog/battery-families/`` — the battery compatibility units (``catalog_battery_family``).

A family referenced by a live battery spec, or named in an inverter's ``compatible_battery_families``, can neither
be deleted nor change its slug (409 ``battery_family_in_use``).
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from catalog.models import BatteryFamily, BatterySpec, InverterSpec
from catalog.services.common import CACHE_NAMESPACE
from core.errors import Conflict
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

SNAPSHOT_FIELDS = ("slug", "name", "voltage_class", "notes")
EDITABLE_FIELDS = SNAPSHOT_FIELDS


def families_queryset():
    return BatteryFamily.objects.order_by("name", "id")


def _slug_taken() -> Conflict:
    return Conflict("battery_family_slug_taken", "Another battery family already uses this slug.", errors={"slug": ["Already in use."]})


def references(family: BatteryFamily) -> int:
    batteries = BatterySpec.objects.filter(family=family, component__deleted_at__isnull=True).count()
    inverters = InverterSpec.objects.filter(compatible_battery_families__contains=[family.slug], component__deleted_at__isnull=True).count()
    return batteries + inverters


@transaction.atomic
def create_family(*, user, data) -> BatteryFamily:
    family = BatteryFamily(**{name: data[name] for name in EDITABLE_FIELDS if name in data})
    stamp_create(family, user)
    try:
        with transaction.atomic():
            family.save()
    except IntegrityError:
        raise _slug_taken() from None
    record("catalog.battery_family_created", obj=family, actor=user, after=snapshot(family, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)
    return family


@transaction.atomic
def update_family(instance: BatteryFamily, *, user, data, expected_version=None) -> BatteryFamily:
    family = BatteryFamily.objects.select_for_update().get(pk=instance.pk)
    check_version(family, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and getattr(family, name) != data[name]}
    if not values:
        return family
    if "slug" in values and references(family):
        raise Conflict("battery_family_in_use", "Batteries or inverters reference this family; its slug cannot change.", errors={"slug": ["In use."]})
    before = snapshot(family, SNAPSHOT_FIELDS)
    try:
        with transaction.atomic():
            family.versioned_update(user, **values)
    except IntegrityError:
        raise _slug_taken() from None
    changed_before, changed_after = changes(before, snapshot(family, SNAPSHOT_FIELDS))
    record("catalog.battery_family_updated", obj=family, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return family


@transaction.atomic
def delete_family(instance: BatteryFamily, *, user, expected_version=None) -> None:
    family = BatteryFamily.objects.select_for_update().get(pk=instance.pk)
    check_version(family, expected_version)
    used = references(family)
    if used:
        raise Conflict("battery_family_in_use", f"{used} batteries or inverters reference this family.", errors={"slug": ["In use."]})
    family.soft_delete(user)
    record("catalog.battery_family_deleted", obj=family, actor=user, before=snapshot(family, SNAPSHOT_FIELDS))
    bump(CACHE_NAMESPACE)
