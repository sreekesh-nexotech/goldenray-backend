"""Reference list shapes: staff (full rows) and public (what the website reads)."""

from __future__ import annotations

from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from reference.models import Appliance, DeviceType, EvCar, EvScooter, KsebTariff, RoomSize, Wattage
from reference.services.lists import APPLIANCES, DEVICE_TYPES, EV_CARS, EV_SCOOTERS, ROOM_SIZES, TARIFFS, WATTAGES, ListSpec

BASE_FIELDS = ["uid", "is_active", "sort_order", "created_at", "updated_at", "version"]

# What the website reads (legacy payload fields, the integer ``id`` replaced by ``uid``) — kept in step with
# docs/decisions/careers-reference.md "Public reference payloads".
PUBLIC_FIELDS = {
    TARIFFS.key: ["uid", "slab_from_units", "slab_to_units", "phase", "rate_per_unit", "fixed_charge", "effective_from"],
    DEVICE_TYPES.key: ["uid", "name", "show_in_ui", "url", "watts"],
    WATTAGES.key: ["uid", "value", "show_in_ui"],
    ROOM_SIZES.key: ["uid", "bhk_type", "size", "units"],
    EV_CARS.key: ["uid", "model", "battery_capacity", "claimed_range", "adjusted_real_world_range", "ex_showroom_price", "energy_consumption"],
    EV_SCOOTERS.key: ["uid", "model", "battery_capacity", "claimed_range", "adjusted_real_world_range", "ex_showroom_price", "energy_consumption"],
    APPLIANCES.key: ["uid", "code", "name", "name_ml", "icon", "watts", "default_hours", "is_optional"],
}

# Per-field write rules the model cannot express to DRF (ranges enforced by DB checks as well). ``validators: []``
# drops DRF's generated UniqueValidator: live uniqueness is the database's job and answers 409 ``<entity>_exists``.
EXTRA_KWARGS = {
    TARIFFS.key: {"phase": {"allow_null": True, "allow_blank": False, "required": False}, "effective_from": {"required": False}},
    DEVICE_TYPES.key: {"k_value": {"min_value": 0}},
    EV_CARS.key: {"battery_capacity": {"min_value": 0}, "energy_consumption": {"min_value": 0}, "k_value": {"min_value": 0}, "ex_showroom_price": {"min_value": 0}},
    EV_SCOOTERS.key: {"battery_capacity": {"min_value": 0}, "energy_consumption": {"min_value": 0}, "k_value": {"min_value": 0}, "ex_showroom_price": {"min_value": 0}},
    WATTAGES.key: {"value": {"min_value": 1, "validators": []}},
    ROOM_SIZES.key: {"bhk_type": {"min_value": 1}, "size": {"min_value": 1}},
    APPLIANCES.key: {"default_hours": {"min_value": 0, "max_value": 24}, "code": {"validators": []}},
}
SORT_ORDER = {"required": False, "min_value": -1000000, "max_value": 1000000}


def _meta(model, fields, extra=None, read_only=False):
    attrs = {"model": model, "fields": fields, "validators": [], "extra_kwargs": extra or {}}
    if read_only:
        attrs["read_only_fields"] = fields
    return type("Meta", (), attrs)


def _build(spec: ListSpec, model, prefix: str):
    staff_fields = ["uid", *spec.fields, *BASE_FIELDS[1:]]
    staff = type(f"{prefix}Serializer", (serializers.ModelSerializer,), {"Meta": _meta(model, staff_fields, read_only=True)})
    public = type(f"Public{prefix}Serializer", (serializers.ModelSerializer,), {"Meta": _meta(model, PUBLIC_FIELDS[spec.key], read_only=True)})
    extra = {**EXTRA_KWARGS.get(spec.key, {}), "sort_order": SORT_ORDER}
    write_fields = [*spec.fields, "is_active", "sort_order"]

    def to_representation(self, instance):
        return staff(instance, context=self.context).data

    create = type(f"{prefix}CreateSerializer", (serializers.ModelSerializer,), {"Meta": _meta(model, write_fields, extra), "to_representation": to_representation})
    update = type(f"{prefix}UpdateSerializer", (ExpectedVersionMixin, create), {"Meta": _meta(model, [*write_fields, "expected_version"], extra)})
    return staff, public, create, update


SERIALIZERS = {
    TARIFFS.key: _build(TARIFFS, KsebTariff, "KsebTariff"),
    DEVICE_TYPES.key: _build(DEVICE_TYPES, DeviceType, "DeviceType"),
    WATTAGES.key: _build(WATTAGES, Wattage, "Wattage"),
    ROOM_SIZES.key: _build(ROOM_SIZES, RoomSize, "RoomSize"),
    EV_CARS.key: _build(EV_CARS, EvCar, "EvCar"),
    EV_SCOOTERS.key: _build(EV_SCOOTERS, EvScooter, "EvScooter"),
    APPLIANCES.key: _build(APPLIANCES, Appliance, "Appliance"),
}
