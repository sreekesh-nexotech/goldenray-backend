"""Staff writes for the flat reference lists (module ``reference_data``): tariffs, device types, wattages, room sizes,
EV cars, EV scooters, appliances. Pincodes (with their post offices) are in ``reference.services.pincodes``.

One implementation, parametrised by a :class:`ListSpec`, so the seven lists behave identically:

* create/update/delete run in one transaction, are audited (``reference.<entity>_created|updated|deleted``) and
  bump the list's cache namespace ``reference:<key>`` (the public endpoints are cached for a day);
* a live-uniqueness clash (device type name, wattage value, room size, EV model, appliance code, tariff slab) is
  409 ``<entity>_exists`` with the offending field;
* delete is a soft delete (``archive`` permission); rows are never hard-deleted;
* a new row without ``sort_order`` goes to the end of its list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial

from django.db import IntegrityError, transaction
from django.db.models import Max

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from reference.models import Appliance, DeviceType, EvCar, EvScooter, KsebTariff, RoomSize, Wattage

COMMON_FIELDS = ("is_active", "sort_order")


@dataclass(frozen=True)
class ListSpec:
    key: str  # URL segment and cache namespace suffix ("device-types")
    entity: str  # audit/error vocabulary ("device_type")
    model: type
    fields: tuple[str, ...]  # editable columns besides is_active/sort_order
    unique_field: str  # the field a live-uniqueness clash is reported on
    search_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def namespace(self) -> str:
        return f"reference:{self.key}"

    @property
    def editable(self) -> tuple[str, ...]:
        return self.fields + COMMON_FIELDS


TARIFFS = ListSpec("tariffs", "kseb_tariff", KsebTariff, ("slab_from_units", "slab_to_units", "phase", "rate_per_unit", "fixed_charge", "effective_from"), "slab_from_units")
DEVICE_TYPES = ListSpec("device-types", "device_type", DeviceType, ("name", "show_in_ui", "url", "watts", "k_value"), "name", ("name",))
WATTAGES = ListSpec("wattages", "wattage", Wattage, ("value", "show_in_ui"), "value")
ROOM_SIZES = ListSpec("room-sizes", "room_size", RoomSize, ("bhk_type", "size", "units"), "size")
_EV_FIELDS = ("model", "battery_capacity", "claimed_range", "adjusted_real_world_range", "ex_showroom_price", "energy_consumption", "k_value")
EV_CARS = ListSpec("ev-cars", "ev_car", EvCar, _EV_FIELDS, "model", ("model",))
EV_SCOOTERS = ListSpec("ev-scooters", "ev_scooter", EvScooter, _EV_FIELDS, "model", ("model",))
APPLIANCES = ListSpec("appliances", "appliance", Appliance, ("code", "name", "name_ml", "icon", "watts", "default_hours", "is_optional"), "code", ("code", "name", "name_ml"))

SPECS: dict[str, ListSpec] = {spec.key: spec for spec in (TARIFFS, DEVICE_TYPES, WATTAGES, ROOM_SIZES, EV_CARS, EV_SCOOTERS, APPLIANCES)}


def queryset(spec: ListSpec):
    return spec.model.objects.all()


def row_snapshot(spec: ListSpec, row) -> dict:
    return snapshot(row, spec.editable)


def integrity_error(entity: str, unique_field: str, exc: IntegrityError) -> DomainError:
    """409 ``<entity>_exists`` for a live-uniqueness clash; 400 for any other rule the database refused."""
    if "_uniq" in str(exc):
        return Conflict(f"{entity}_exists", "Another live row already has this value.", errors={unique_field: ["Already exists."]})
    return DomainError("validation_error", "The values break a data rule.", errors={"non_field_errors": ["The values break a data rule."]})


def _validate(spec: ListSpec, values: dict) -> None:
    if spec is TARIFFS:
        low, high = values.get("slab_from_units"), values.get("slab_to_units")
        if low is not None and high is not None and high < low:
            raise DomainError("validation_error", "The slab ends before it starts.", errors={"slab_to_units": ["Must be at least slab_from_units."]})


def next_sort_order(model) -> int:
    return (model.objects.aggregate(top=Max("sort_order"))["top"] or 0) + 1


@transaction.atomic
def create_row(spec: ListSpec, *, user, data):
    values = {name: data[name] for name in spec.editable if name in data}
    if values.get("sort_order") is None:
        values["sort_order"] = next_sort_order(spec.model)
    _validate(spec, values)
    row = spec.model(**values)
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:
        raise integrity_error(spec.entity, spec.unique_field, exc) from None
    record(f"reference.{spec.entity}_created", obj=row, actor=user, after=row_snapshot(spec, row))
    bump(spec.namespace)
    return row


@transaction.atomic
def update_row(spec: ListSpec, instance, *, user, data, expected_version=None):
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    before = row_snapshot(spec, row)
    values = {name: data[name] for name in spec.editable if name in data and data[name] != getattr(row, name)}
    if not values:
        return row
    _validate(spec, {**{name: getattr(row, name) for name in spec.editable}, **values})
    try:
        with transaction.atomic():
            row.versioned_update(user, **values)
    except IntegrityError as exc:
        raise integrity_error(spec.entity, spec.unique_field, exc) from None
    changed_before, changed_after = changes(before, row_snapshot(spec, row))
    record(f"reference.{spec.entity}_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(spec.namespace)
    return row


@transaction.atomic
def delete_row(spec: ListSpec, instance, *, user, expected_version=None) -> None:
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    row.soft_delete(user)
    record(f"reference.{spec.entity}_deleted", obj=row, actor=user, before=row_snapshot(spec, row))
    bump(spec.namespace)


def services_for(spec: ListSpec) -> dict:
    """The ``services`` map a staff viewset declares (``core.views.mixins``)."""
    return {"create": partial(create_row, spec), "update": partial(update_row, spec), "destroy": partial(delete_row, spec)}
