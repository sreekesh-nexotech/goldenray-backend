"""Old ``/api/solar-panels/``, ``/api/solar-inverters/`` and ``/api/batteries/`` (legacy ``goldenray`` views).

The rows are the website's published products (``catalog.services.public``) rebuilt in the legacy serializer shapes
(the mapping of docs/decisions/catalog.md; ``catalog/tests/legacy_rebuild.py`` is its checked form). The legacy
query parameters are applied to the rebuilt rows exactly as the legacy ORM filters were (``type``, ``rating``,
``minEfficiency``, ``brand``, ``ids`` …; ``sort`` any column, ``order`` asc/desc, NULLs last ascending / first
descending like Postgres; ties in legacy-id order). The legacy server answered HTTP 500 for a malformed number or an
unknown sort column; the shim answers 400 ``{"error": …}`` (DV).
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from catalog.models import Component
from catalog.services import public
from catalog.services.common import PUBLIC_CACHE_NAMESPACES
from catalog.services.legacy_import import BATTERIES, INVERTERS, OVERALL_RATINGS, PANEL_TYPES, PANELS, RATING_TIERS, TECHNOLOGIES
from core.errors import DomainError
from legacy.services.ids import BACKEND, legacy_ids

_DATETIME = serializers.DateTimeField()
PANEL_TYPE_NAMES = {value: key for key, value in PANEL_TYPES.items()}
TECHNOLOGY_NAMES = {value: key for key, value in TECHNOLOGIES.items()}
OVERALL_NAMES = {value: key for key, value in OVERALL_RATINGS.items()}
RATING_TIER_NAMES = {value: key for key, value in RATING_TIERS.items()}
CACHE_NAMESPACES = PUBLIC_CACHE_NAMESPACES


def _money(value, places: int = 2) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def _stamps(profile) -> dict:
    return {"created_at": _DATETIME.to_representation(profile.created_at), "updated_at": _DATETIME.to_representation(profile.updated_at)}


def panel_row(legacy_id: int, profile) -> dict:
    component = profile.component
    spec, ratings = component.panel_spec, profile.ratings or {}
    return {
        "id": legacy_id,
        "brand": component.brand_label,
        "name": profile.headline,
        "wattage": spec.wattage_w,
        "panel_type": PANEL_TYPE_NAMES.get(spec.panel_type),
        "technology": TECHNOLOGY_NAMES.get(spec.technology),
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
        "overall_rating": OVERALL_NAMES.get(profile.overall_rating),
        **_stamps(profile),
    }


def _inverter_type(spec) -> str | None:
    if spec.inverter_type == "HYBRID":
        return "hybrid"
    return {"STRING": "string", "MICRO": "microinverter", "OPTIMIZED_STRING": "optimized-string"}.get(spec.topology)


def _watts(kw) -> int | None:
    return None if kw is None else int(kw * 1000)


def inverter_row(legacy_id: int, profile) -> dict:
    component = profile.component
    spec, ratings = component.inverter_spec, profile.ratings or {}
    return {
        "id": legacy_id,
        "brand": component.brand_label,
        "name": profile.headline,
        "inverter_type": _inverter_type(spec),
        "rating_tier": RATING_TIER_NAMES.get(profile.rating_tier),
        "image_url": profile.image_url,
        "description": profile.summary,
        "rated_output_power": _watts(spec.kw),
        "maximum_dc_input": _watts(spec.max_dc_input_kw),
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
        "overall_rating": OVERALL_NAMES.get(profile.overall_rating),
        **_stamps(profile),
    }


def _with_ids(category: str, table: str) -> list[tuple[int, object]]:
    profiles = list(public.products_in(category))
    ids = legacy_ids(Component, [profile.component_id for profile in profiles], system=BACKEND, table=table)
    return [(ids[profile.component_id], profile) for profile in profiles]


def battery_rows() -> list[dict]:
    """Legacy ``Battery.objects.all()`` (no ordering: the heap, i.e. by id) with the current LIST price."""
    pairs = _with_ids("batteries", BATTERIES)
    prices = public.price_context([profile for _, profile in pairs])
    rows = []
    for legacy_id, profile in pairs:
        spec, info = profile.component.battery_spec, prices.get(profile.component_id)
        rows.append({"id": legacy_id, "battery_capacity": _money(spec.capacity_kwh), "backup_hour": _money(spec.backup_hours), "battery_price": _money(info.min_amount if info else None)})
    return sorted(rows, key=lambda row: row["id"])


# ── the legacy query parameters ─────────────────────────────────────────────────────────────────────────────────────
def _bad(message: str) -> DomainError:
    return DomainError("invalid_parameter", message)


def _number(params, name: str, cast):
    raw = params.get(name)
    if not raw:
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise _bad(f"Invalid value for {name}: {raw!r}") from None


def _csv(params, name: str) -> list[str] | None:
    raw = params.get(name)
    return [item.strip() for item in raw.split(",")] if raw else None


def _compare(rows, field, bound, keep):
    if bound is None:
        return rows
    return [row for row in rows if row[field] is not None and keep(Decimal(str(row[field])), Decimal(str(bound)))]


def _within(rows, field, values):
    return rows if values is None else [row for row in rows if row[field] in values]


def _ids(rows, params):
    raw = params.get("ids")
    if not raw:
        return rows
    try:
        wanted = {int(item.strip()) for item in raw.split(",")}
    except ValueError:
        raise _bad(f"Invalid value for ids: {raw!r}") from None
    return [row for row in rows if row["id"] in wanted]


def _key(value):
    """Postgres order of one column's values: numbers (and numeric strings of DecimalFields) by value, text as text."""
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, Decimal(str(value)))
    if isinstance(value, str):
        return (1, value)
    return (2, str(value))


def _decimal_key(value):
    try:
        return (0, Decimal(value))
    except (ArithmeticError, TypeError, ValueError):
        return _key(value)


def _sorted(rows: list[dict], params, mapping: dict[str, str], decimals: set[str]) -> list[dict]:
    field = params.get("sort", "kerala_climate_score")
    field = mapping.get(field, field)
    descending = params.get("order", "desc") == "desc"
    if field not in PANEL_COLUMNS | INVERTER_COLUMNS or (rows and field not in rows[0]):
        raise _bad(f"Cannot sort by {field!r}.")
    key = _decimal_key if field in decimals else _key
    present = sorted((row for row in rows if row.get(field) is not None), key=lambda row: (key(row[field]), row["id"]))
    if descending:
        runs: list[list[dict]] = []
        for row in present:
            if runs and key(runs[-1][0][field]) == key(row[field]):
                runs[-1].append(row)
            else:
                runs.append([row])
        present = [row for run in reversed(runs) for row in run]  # descending by value, ties still by id
    missing = [row for row in rows if row.get(field) is None]
    return missing + present if descending else present + missing


PANEL_SORTS = {"topRated": "kerala_climate_score", "keralaClimateScore": "kerala_climate_score", "efficiency": "efficiency", "price": "price_range", "warranty": "performance_warranty"}
INVERTER_SORTS = {"topRated": "kerala_climate_score", "keralaClimateScore": "kerala_climate_score", "efficiency": "maximum_efficiency", "price": "price_range", "warranty": "warranty_years"}
PANEL_DECIMALS = {"efficiency", "temperature_coefficient", "weight", "first_year_power_drop", "annual_degradation", "output_at_year_25"}
INVERTER_DECIMALS = {"weight", "maximum_efficiency", "european_efficiency", "mppt_efficiency"}
_SCALARS = {"id", "brand", "name", "image_url", "description", "price_range", "kerala_climate_score", "overall_rating", "created_at", "updated_at", "ip_rating", "weight"}
PANEL_COLUMNS = (
    _SCALARS
    | PANEL_DECIMALS
    | {"wattage", "panel_type", "technology", "noct", "real_output_at_60c", "wind_load", "moisture_protection", "bifacial_gain", "product_warranty"}
    | {
        "performance_warranty",
        "manufacturing_capacity",
        "bloomberg_tier1",
        "pvel_top_performer",
        "bis_certified",
        "independent_audit",
        "subsidy_eligible",
        "efficiency_rating",
        "heat_performance_rating",
        "warranty_rating",
        "kerala_climate_rating",
    }
)
INVERTER_COLUMNS = (
    _SCALARS
    | INVERTER_DECIMALS
    | {"inverter_type", "rating_tier", "rated_output_power", "maximum_dc_input", "mppt_trackers", "maximum_dc_voltage", "warranty_years"}
    | {
        "extendable_warranty_years",
        "year_founded",
        "dc_oversizing",
        "ac_overloading",
        "efficiency_rating",
        "reliability_rating",
        "warranty_rating",
        "kerala_climate_rating",
        "suitable_system_size",
        "display",
        "cooling",
        "noise_level",
        "operating_temperature",
        "maximum_input_current",
        "brand_trust",
        "countries_served",
        "global_installations",
    }
)


def panels(params) -> dict:
    rows = [panel_row(legacy_id, profile) for legacy_id, profile in _with_ids("panels", PANELS)]
    rows = _within(rows, "panel_type", _csv(params, "type"))
    rows = _within(rows, "overall_rating", _csv(params, "rating"))
    rows = _compare(rows, "efficiency", _number(params, "minEfficiency", float), lambda value, bound: value >= bound)
    rows = _compare(rows, "efficiency", _number(params, "maxEfficiency", float), lambda value, bound: value <= bound)
    rows = _compare(rows, "product_warranty", _number(params, "minProductWarranty", int), lambda value, bound: value >= bound)
    rows = _compare(rows, "performance_warranty", _number(params, "minPerformanceWarranty", int), lambda value, bound: value >= bound)
    rows = _within(rows, "brand", _csv(params, "brand"))
    rows = _compare(rows, "kerala_climate_score", _number(params, "minKeralaScore", int), lambda value, bound: value >= bound)
    rows = _sorted(_ids(rows, params), params, PANEL_SORTS, PANEL_DECIMALS)
    return {"data": rows, "meta": {"total": len(rows)}}


def inverters(params) -> dict:
    rows = [inverter_row(legacy_id, profile) for legacy_id, profile in _with_ids("inverters", INVERTERS)]
    rows = _within(rows, "inverter_type", _csv(params, "type"))
    rows = _within(rows, "rating_tier", _csv(params, "tier"))
    rows = _within(rows, "overall_rating", _csv(params, "rating"))
    rows = _compare(rows, "warranty_years", _number(params, "minWarranty", int), lambda value, bound: value >= bound)
    rows = _compare(rows, "extendable_warranty_years", _number(params, "extendableTo", int), lambda value, bound: value >= bound)
    rows = _within(rows, "brand", _csv(params, "brand"))
    rows = _compare(rows, "kerala_climate_score", _number(params, "minKeralaScore", int), lambda value, bound: value >= bound)
    rows = _sorted(_ids(rows, params), params, INVERTER_SORTS, INVERTER_DECIMALS)
    return {"data": rows, "meta": {"total": len(rows)}}
