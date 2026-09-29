"""Reference rebuild of the legacy website product responses from the catalog tables (parity evidence).

The legacy shim (``legacy/``, a later package) serves ``/legacy/api/solar-panels/``, ``/solar-inverters/`` and
``/batteries/`` with exactly these shapes; this module is the executable form of the mapping tables in
docs/decisions/catalog.md and is checked against the captured legacy responses in ``test_legacy_parity.py``.

* the legacy ``id`` is the ``core_legacy_map`` source id of the component;
* decimals are printed with the legacy column scale (DRF ``DecimalField`` strings), timestamps in IST like the
  legacy server, both from the public profile's ``created_at`` / ``updated_at``;
* the list order is the legacy default (``-kerala_climate_score``); the legacy server leaves ties in heap order, the
  rebuild orders them by legacy id;
* ``battery_price`` is the LIST price — pricing imports it from the importer's returned price rows, so the rebuild
  takes a ``prices`` mapping ``{sku: amount}``.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from catalog.models import Component
from catalog.services.legacy_import import BATTERIES, INVERTERS, OVERALL_RATINGS, PANEL_TYPES, PANELS, RATING_TIERS, TECHNOLOGIES
from core.models import LegacyMap

_DATETIME = serializers.DateTimeField()
PANEL_TYPE_NAMES = {value: key for key, value in PANEL_TYPES.items()}
TECHNOLOGY_NAMES = {value: key for key, value in TECHNOLOGIES.items()}
OVERALL_NAMES = {value: key for key, value in OVERALL_RATINGS.items()}
RATING_TIER_NAMES = {value: key for key, value in RATING_TIERS.items()}


def _money(value, places: int = 2) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def _components(table: str) -> list[tuple[int, Component]]:
    rows = []
    for entry in LegacyMap.objects.filter(source_system="BACKEND", source_table=table):
        component = Component.objects.select_related("brand", "category", "panel_spec", "inverter_spec", "battery_spec", "public_profile").get(pk=entry.target_id)
        rows.append((int(entry.source_id), component))
    return rows


def panel_row(legacy_id: int, component: Component) -> dict:
    spec, profile = component.panel_spec, component.public_profile
    ratings = profile.ratings or {}
    return {
        "id": legacy_id,
        "brand": component.brand_label,
        "name": profile.headline,
        "wattage": spec.wattage_w,
        "panel_type": PANEL_TYPE_NAMES[spec.panel_type],
        "technology": TECHNOLOGY_NAMES[spec.technology],
        "image_url": profile.image_url,
        "description": profile.summary,
        "efficiency": _money(spec.efficiency_pct),
        "temperature_coefficient": _money(spec.temperature_coefficient),
        "noct": spec.noct_c,
        "real_output_at_60c": spec.real_output_at_60c_pct,
        "ip_rating": spec.ip_rating,
        "wind_load": spec.wind_load_pa,
        "moisture_protection": spec.moisture_protection,
        "weight": _money(spec.weight_kg),
        "bifacial_gain": spec.bifacial_gain_pct,
        "product_warranty": component.warranty_product_years,
        "performance_warranty": component.warranty_performance_years,
        "first_year_power_drop": _money(spec.first_year_drop_pct),
        "annual_degradation": _money(spec.annual_degradation_pct),
        "output_at_year_25": _money(spec.output_at_year_25_pct),
        "manufacturing_capacity": spec.manufacturing_capacity,
        "bloomberg_tier1": spec.bloomberg_tier1,
        "pvel_top_performer": spec.pvel_top_performer,
        "bis_certified": spec.bis_certified,
        "independent_audit": spec.independent_audit,
        "certifications": spec.certifications,
        "price_range": profile.price_range_label,
        "subsidy_eligible": profile.subsidy_eligible,
        "kerala_climate_score": profile.kerala_climate_score,
        "efficiency_rating": ratings.get("efficiency"),
        "heat_performance_rating": ratings.get("heat_performance"),
        "warranty_rating": ratings.get("warranty"),
        "kerala_climate_rating": ratings.get("kerala_climate"),
        "overall_rating": OVERALL_NAMES[profile.overall_rating],
        "created_at": _DATETIME.to_representation(profile.created_at),
        "updated_at": _DATETIME.to_representation(profile.updated_at),
    }


def _legacy_inverter_type(spec) -> str:
    if spec.inverter_type == "HYBRID":
        return "hybrid"
    return {"STRING": "string", "MICRO": "microinverter", "OPTIMIZED_STRING": "optimized-string"}[spec.topology]


def inverter_row(legacy_id: int, component: Component) -> dict:
    spec, profile = component.inverter_spec, component.public_profile
    ratings = profile.ratings or {}
    return {
        "id": legacy_id,
        "brand": component.brand_label,
        "name": profile.headline,
        "inverter_type": _legacy_inverter_type(spec),
        "rating_tier": RATING_TIER_NAMES[profile.rating_tier],
        "image_url": profile.image_url,
        "description": profile.summary,
        "rated_output_power": int(spec.kw * 1000),
        "maximum_dc_input": int(spec.max_dc_input_kw * 1000),
        "mppt_trackers": spec.mppt_count,
        "maximum_dc_voltage": spec.max_pv_voltage_v,
        "maximum_input_current": spec.max_input_current_text,
        "weight": _money(spec.weight_kg),
        "display": spec.display,
        "suitable_system_size": spec.suitable_system_size,
        "maximum_efficiency": _money(spec.efficiency_pct),
        "european_efficiency": _money(spec.european_efficiency_pct),
        "mppt_efficiency": _money(spec.mppt_efficiency_pct),
        "dc_oversizing": spec.dc_oversizing_pct,
        "ac_overloading": spec.ac_overloading_pct,
        "pid_protection": spec.pid_protection,
        "iv_curve_scanning": spec.iv_curve_scanning,
        "ip_rating": spec.ip_rating,
        "corrosion_protection": spec.corrosion_protection,
        "operating_temperature": spec.operating_temperature,
        "cooling": spec.cooling,
        "noise_level": spec.noise_level,
        "dc_surge_protection": spec.dc_surge_protection,
        "ac_surge_protection": spec.ac_surge_protection,
        "arc_fault_detection": spec.arc_fault_detection,
        "grid_protection": spec.grid_protection,
        "monitoring_app": spec.monitoring_app,
        "real_time_monitoring": spec.real_time_monitoring,
        "remote_diagnostics": spec.remote_diagnostics,
        "firmware_updates": spec.firmware_updates,
        "connectivity": spec.connectivity,
        "warranty_years": component.warranty_product_years,
        "extendable_warranty_years": component.warranty_extendable_years,
        "certifications": spec.certifications,
        "brand_trust": spec.brand_trust,
        "year_founded": spec.year_founded,
        "countries_served": spec.countries_served,
        "global_installations": spec.global_installations,
        "price_range": profile.price_range_label,
        "kerala_climate_score": profile.kerala_climate_score,
        "efficiency_rating": ratings.get("efficiency"),
        "reliability_rating": ratings.get("reliability"),
        "warranty_rating": ratings.get("warranty"),
        "kerala_climate_rating": ratings.get("kerala_climate"),
        "overall_rating": OVERALL_NAMES[profile.overall_rating],
        "created_at": _DATETIME.to_representation(profile.created_at),
        "updated_at": _DATETIME.to_representation(profile.updated_at),
    }


def battery_row(legacy_id: int, component: Component, prices: dict[str, Decimal]) -> dict:
    spec = component.battery_spec
    return {"id": legacy_id, "battery_capacity": _money(spec.capacity_kwh), "backup_hour": _money(spec.backup_hours), "battery_price": _money(prices.get(component.sku))}


def _ordered(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (-row["kerala_climate_score"], row["id"]))


def panels_response() -> dict:
    data = _ordered([panel_row(legacy_id, component) for legacy_id, component in _components(PANELS)])
    return {"data": data, "meta": {"total": len(data)}}


def inverters_response() -> dict:
    data = _ordered([inverter_row(legacy_id, component) for legacy_id, component in _components(INVERTERS)])
    return {"data": data, "meta": {"total": len(data)}}


def batteries_response(prices: dict[str, Decimal]) -> list[dict]:
    """Legacy ``Battery.objects.all()`` — no ordering: by id, as the legacy heap returns them."""
    return sorted((battery_row(legacy_id, component, prices) for legacy_id, component in _components(BATTERIES)), key=lambda row: row["id"])
