"""Old ``/api/<list>/`` reference payloads (legacy ``goldenray`` ModelSerializers, ``Model.objects.all()``, no paging).

Served from the live, active rows of the reference package (``reference.services.lists``) in legacy-id order; the old
columns are rebuilt from the typed ones (tariff ``min_units``/``max_units``/``rate`` ← ``slab_from_units``/``slab_to_units``/
``rate_per_unit`` at the legacy 2-decimal scale; EV ``ex_showroom_price`` as the legacy integer). Pincodes are the post
office rows (``reference_pincode_office``), as the legacy table was.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from legacy.services.ids import BACKEND, legacy_ids
from reference.models import PincodeOffice
from reference.services import lists
from reference.services.pincodes import CACHE_NAMESPACE as PINCODES_NAMESPACE


def _money(value, places: int = 2) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def _int_or_decimal(value):
    if value is None:
        return None
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


@dataclass(frozen=True)
class LegacyList:
    table: str  # legacy table (core_legacy_map.source_table)
    queryset: Callable
    row: Callable  # (legacy id, row) -> dict in the legacy serializer's field order
    namespaces: tuple[str, ...]


def _device_type(legacy_id, row):
    return {"id": legacy_id, "name": row.name, "show_in_ui": row.show_in_ui, "url": row.url, "watts": row.watts}


def _wattage(legacy_id, row):
    return {"id": legacy_id, "value": row.value, "show_in_ui": row.show_in_ui}


def _room_size(legacy_id, row):
    return {"id": legacy_id, "bhk_type": row.bhk_type, "size": row.size, "units": row.units}


def _ev(legacy_id, row):
    return {
        "id": legacy_id,
        "model": row.model,
        "battery_capacity": row.battery_capacity,
        "claimed_range": row.claimed_range,
        "adjusted_real_world_range": row.adjusted_real_world_range,
        "ex_showroom_price": _int_or_decimal(row.ex_showroom_price),
        "energy_consumption": row.energy_consumption,
    }


def _tariff(legacy_id, row):
    return {"id": legacy_id, "min_units": row.slab_from_units, "max_units": row.slab_to_units, "rate": _money(row.rate_per_unit)}


def _pincode(legacy_id, row):
    return {"id": legacy_id, "pincode": row.pincode.pincode, "state": row.state, "district": row.district, "office_name": row.office_name, "region": row.region, "division": row.division}


def _active(spec):
    return lambda: lists.queryset(spec).filter(is_active=True)


LISTS: dict[str, LegacyList] = {
    "device-types": LegacyList("device_types", _active(lists.DEVICE_TYPES), _device_type, (lists.DEVICE_TYPES.namespace,)),
    "wattages": LegacyList("wattages", _active(lists.WATTAGES), _wattage, (lists.WATTAGES.namespace,)),
    "room-sizes": LegacyList("room_size", _active(lists.ROOM_SIZES), _room_size, (lists.ROOM_SIZES.namespace,)),
    "ev-cars": LegacyList("ev_cars", _active(lists.EV_CARS), _ev, (lists.EV_CARS.namespace,)),
    "ev-scooters": LegacyList("ev_scooters", _active(lists.EV_SCOOTERS), _ev, (lists.EV_SCOOTERS.namespace,)),
    "tariffs": LegacyList("kseb_tariffs", _active(lists.TARIFFS), _tariff, (lists.TARIFFS.namespace,)),
    "pincodes": LegacyList("pincodes", lambda: PincodeOffice.objects.filter(pincode__deleted_at__isnull=True, pincode__is_active=True).select_related("pincode"), _pincode, (PINCODES_NAMESPACE,)),
}


def legacy_list(key: str) -> list[dict]:
    spec = LISTS[key]
    rows = list(spec.queryset())
    ids = legacy_ids(spec.queryset().model, [row.pk for row in rows], system=BACKEND, table=spec.table)
    return sorted((spec.row(ids[row.pk], row) for row in rows), key=lambda item: item["id"])
