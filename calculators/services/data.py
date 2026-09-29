"""The tables the website calculators read, as one cached :class:`engines.website_calculators.CalculatorData`.

The legacy views queried the database on every request (a device type per device, a tariff per step). Here the
tables are loaded once per change into a snapshot cached under the namespaces of everything in it — the
calculators' sizing tables (``calculators:sizes``), the reference lists (tariffs, device types, EVs, pincodes), the
catalog's battery products and their prices (``catalog``, ``pricing``) — plus the day, because the tariff schedule in
force changes at midnight. A warm calculation costs no query; any staff write is visible at once.

Reference data is read through ``reference.services.lookups`` (the documented reads); the KSEB schedule is the one
in force today for a single-phase domestic connection (the legacy calculators knew one schedule, which imports as
the every-phase schedule).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from calculators.models import BillRangeSize, CapacitySize, PropertyType
from calculators.services import batteries
from calculators.services.sizing import CACHE_NAMESPACE
from engines import website_calculators as engine
from flarize.cache_utils import build_key, get_versions
from reference.services import lookups
from reference.services.pincodes import CACHE_NAMESPACE as PINCODES_NAMESPACE

logger = logging.getLogger("flarize.calculators")

SNAPSHOT_TTL_SECONDS = 300
TARIFF_PHASE = "1P"
REFERENCE_NAMESPACES = ("reference:tariffs", "reference:device-types", "reference:ev-cars", "reference:ev-scooters", PINCODES_NAMESPACE)
PERCENT = Decimal("0.01")


def cache_namespaces() -> list[str]:
    return [CACHE_NAMESPACE, *REFERENCE_NAMESPACES, *batteries.CACHE_NAMESPACES]


def _percent(fraction: Decimal | None) -> Decimal | None:
    """A stored rate fraction as the legacy percentage column (``0.0650`` → ``6.50``)."""
    return None if fraction is None else (fraction * 100).quantize(PERCENT)


def load_data(on: dt.date) -> engine.CalculatorData:
    cars, scooters = lookups.active_vehicles()
    labels = dict(PropertyType.choices)
    return engine.CalculatorData(
        # ``position`` reproduces the legacy primary-key order (``sort_order`` is the legacy id) for ``.first()``.
        tariffs=tuple(
            engine.TariffSlab(min_units=row.slab_from_units, max_units=row.slab_to_units, rate=row.rate_per_unit, position=index)
            for index, row in enumerate(sorted(lookups.current_tariffs(on=on, phase=TARIFF_PHASE), key=lambda row: (row.sort_order, row.pk)))
        ),
        capacity_sizes=tuple(
            engine.CapacitySize(
                power_capacity=row.power_capacity_kw,
                time_to_complete=row.installation_days,
                total_cost=row.total_cost,
                total_subsidy=row.total_subsidy,
                area_required=row.area_required_sqft,
            )
            for row in CapacitySize.objects.filter(is_active=True).order_by("power_capacity_kw", "id")
        ),
        bill_range_sizes=tuple(
            engine.BillRangeSize(
                bill_range=row.bill_range,
                property_type=labels[row.property_type],
                power_capacity=row.power_capacity_kw,
                time_to_complete=row.installation_days_range,
                total_cost=row.total_cost,
                total_subsidy=row.total_subsidy,
                area_required=row.area_required_sqft,
                loan_available=row.loan_available,
                per_kw_rate=row.per_kw_rate,
                final_cost=row.final_cost,
                interest_rate=_percent(row.interest_rate),
                inverter_price=row.inverter_price,
                position=row.pk,
            )
            for row in BillRangeSize.objects.filter(is_active=True).order_by("bill_range", "id")
        ),
        device_types=tuple(engine.DeviceType(name=row.name, watts=row.watts, k_value=row.k_value, position=index) for index, row in enumerate(lookups.active_device_types())),
        ev_cars=tuple(engine.Vehicle(model=row.model, energy_consumption=row.energy_consumption, k_value=row.k_value, position=index) for index, row in enumerate(cars)),
        ev_scooters=tuple(engine.Vehicle(model=row.model, energy_consumption=row.energy_consumption, k_value=row.k_value, position=index) for index, row in enumerate(scooters)),
        batteries=batteries.backup_batteries(),
        pincodes=lookups.active_pincode_codes(),
    )


def calculator_data(on: dt.date | None = None) -> engine.CalculatorData:
    """The snapshot, from the cache while none of its namespaces changed (fail-soft on a cache outage)."""
    on = on or timezone.localdate()
    key = build_key("calculators:snapshot", on.isoformat(), versions=get_versions(cache_namespaces()))
    try:
        data = cache.get(key)
    except Exception:  # noqa: BLE001 - a cache outage must not break the calculators
        logger.warning("calculators snapshot cache read failed", exc_info=True)
        data = None
    if data is None:
        data = load_data(on)
        try:
            cache.set(key, data, SNAPSHOT_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("calculators snapshot cache write failed", exc_info=True)
    return data
