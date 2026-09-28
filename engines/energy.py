"""Energy engine: a KSEB bill to consumption, system size, generation and the effective tariff.

Port of Flarize ``src/lib/energyEngine.js`` (``ENERGY_ENGINE_VERSION 'energyEngine.2'``, spec §2) — a protected
formula (spec §19): KSEB LT-1A domestic, bi-monthly billing; telescopic slabs up to the non-telescopic threshold
(500 bi-monthly units), above it the **whole** consumption at one band rate; fixed charge by consumption band and
phase; meter rent by phase; electricity duty on the energy charge only, computed on the unrounded energy charge.

Decisions (``docs/decisions/engines-core.md``):

* **D-8 — the ``'3P'`` tariff fix.** The JavaScript selects the three-phase tariff only when the phase text contains
  ``three``, so the BOM/engineering code ``'3P'`` is billed as single phase. :func:`resolve_phase` with
  ``fix_three_phase_tariff=True`` (the default, PLAN §9 D-8) also maps ``3P``/``3 ph``/``3-phase`` to three phase;
  ``False`` reproduces the JavaScript exactly.
* **The bill→units search** (:func:`bill_to_units`) is a bisection over binary64 midpoints whose last steps depend
  on IEEE-754 rounding; where a bill total jumps exactly at a half unit (e.g. duty ₹1207.5 at 1312.5 units) an exact
  decimal bisection lands on the other side of the half unit and returns one unit less or more. The search is
  therefore a bit-for-bit binary64 replica of the JavaScript (the only floats in the core engines); it returns a
  whole number of units, and every published amount is recomputed from those units in Decimal (:func:`units_to_bill`).
* Everything else is exact Decimal arithmetic, rounded with ``Math.round`` where the JavaScript rounds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from engines.money import ZERO, canonical, exact, is_number, js_round, json_number, round_places, to_decimal

ENERGY_ENGINE_VERSION = "energyEngine.2"
ENERGY_SOURCE = "ENERGY_ENGINE"
ENERGY_STATUS_LIVE = "LIVE"
ENERGY_STATUS_BLOCKED = "BLOCKED"


class EnergyError(StrEnum):
    NO_REGION_CONFIG = "NO_REGION_CONFIG"
    INVALID_BILL_AMOUNT = "INVALID_BILL_AMOUNT"
    NO_YIELD_CONFIG = "NO_YIELD_CONFIG"
    NO_TARIFF_CONFIG = "NO_TARIFF_CONFIG"


class Phase(StrEnum):
    SINGLE = "single"
    THREE = "three"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"


class BillingCategory(StrEnum):
    TELESCOPIC = "telescopic"
    NON_TELESCOPIC = "nonTelescopic"


#: JavaScript fallbacks for configurations in the old shapes (spec §2 "Fallbacks").
DEFAULT_NON_TELESCOPIC_THRESHOLD = Decimal(500)
DEFAULT_NON_TELESCOPIC_RATE = Decimal("8.20")
DEFAULT_FIXED_CHARGE_BI_MONTHLY = Decimal(80)
DEFAULT_METER_RENT_SINGLE = Decimal(12)
DEFAULT_METER_RENT_THREE = Decimal(30)
DEFAULT_METER_RENT_FLAT = Decimal(14)
DEFAULT_DUTY_PCT = Decimal(10)
DEFAULT_COVERAGE_TARGET = Decimal("0.85")
#: The reverse search: bisection over [0, 3000] bi-monthly units, 50 steps (consumption is capped at 3000).
SEARCH_UPPER_UNITS = 3000
SEARCH_STEPS = 50

AVERAGE_TARIFF_RATE_NOTE = "Effective customer bill rate (total bill / units). Includes fixed charge, duty, meter rent. NOT the KSEB energy-only tariff."

_THREE_PHASE_CODE = re.compile(r"3\s*[-_ ]?\s*p(h(ase)?)?")


def resolve_phase(phase: Any, *, fix_three_phase_tariff: bool = True) -> Phase:
    """The tariff phase of a connection-phase text.

    JavaScript: ``String(phase || '').toLowerCase().includes('three') ? 'three' : 'single'`` — ``'3P'`` is single.
    With ``fix_three_phase_tariff`` (D-8, default) ``3P``, ``3p``, ``3 ph``, ``3-phase`` are three phase as well.
    """
    text = str(phase).lower() if phase else ""
    if "three" in text:
        return Phase.THREE
    if fix_three_phase_tariff and _THREE_PHASE_CODE.fullmatch(text.strip()):
        return Phase.THREE
    return Phase.SINGLE


# ---- configuration ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TariffSlab:
    """A telescopic slab: units up to ``up_to_units`` (cumulative, bi-monthly) at ``rate_per_unit``."""

    up_to_units: Decimal | None
    rate_per_unit: Decimal


@dataclass(frozen=True)
class RateBand:
    """A non-telescopic band: the whole consumption at ``rate_per_unit`` when it is ≤ ``up_to_units`` (None = above all)."""

    up_to_units: Decimal | None
    rate_per_unit: Decimal


@dataclass(frozen=True)
class FixedChargeSlab:
    up_to_units: Decimal | None
    charge_bi_monthly: Decimal


@dataclass(frozen=True)
class MeterRent:
    single_phase_bi_monthly: Decimal | None = None
    three_phase_bi_monthly: Decimal | None = None


@dataclass(frozen=True)
class YieldAssumption:
    daily_gen_per_kw: Decimal | None
    unit: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class SizingRule:
    coverage_target: Decimal | None = None
    note: str | None = None


@dataclass(frozen=True)
class RegionConfig:
    """One region of ``energy-config.json`` (``regions.<id>``). ``None`` fields take the JavaScript fallbacks.

    ``meter_rent`` ``None`` means the old flat shape (``meter_rent_bi_monthly``); empty fixed-charge tables fall back
    to ``fixed_charge_bi_monthly``; no non-telescopic bands fall back to ``non_telescopic_rate``.
    """

    region_name: str | None = None
    discom: str | None = None
    tariff_name: str | None = None
    tariff_version: str | None = None
    tariff_slabs: tuple[TariffSlab, ...] = ()
    non_telescopic_threshold: Decimal | None = None
    non_telescopic_slabs: tuple[RateBand, ...] = ()
    non_telescopic_rate: Decimal | None = None
    fixed_charge_single_phase: tuple[FixedChargeSlab, ...] = ()
    fixed_charge_three_phase: tuple[FixedChargeSlab, ...] = ()
    fixed_charge_bi_monthly: Decimal | None = None
    meter_rent: MeterRent | None = None
    meter_rent_bi_monthly: Decimal | None = None
    electricity_duty_pct: Decimal | None = None
    yield_assumption: YieldAssumption | None = None
    sizing: SizingRule | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RegionConfig:
        fixed = data.get("fixedChargeSlabs") if isinstance(data.get("fixedChargeSlabs"), Mapping) else {}
        meter = data.get("meterRent")
        yield_data = data.get("yield")
        sizing = data.get("sizing")
        daily = yield_data.get("dailyGenPerKw") if isinstance(yield_data, Mapping) else None
        return cls(
            region_name=data.get("regionName"),
            discom=data.get("discom"),
            tariff_name=data.get("tariffName"),
            tariff_version=data.get("tariffVersion"),
            tariff_slabs=tuple(TariffSlab(json_number(slab.get("upToUnits")), json_number(slab.get("ratePerUnit"))) for slab in _list(data.get("tariffSlabs"))),
            non_telescopic_threshold=json_number(data.get("nonTelescopicThreshold")),
            non_telescopic_slabs=tuple(RateBand(json_number(slab.get("upToUnits")), json_number(slab.get("ratePerUnit"))) for slab in _list(data.get("nonTelescopicSlabs"))),
            non_telescopic_rate=json_number(data.get("nonTelescopicRate")),
            fixed_charge_single_phase=tuple(FixedChargeSlab(json_number(slab.get("upToUnits")), json_number(slab.get("chargeBiMonthly"))) for slab in _list(fixed.get("singlePhase"))),
            fixed_charge_three_phase=tuple(FixedChargeSlab(json_number(slab.get("upToUnits")), json_number(slab.get("chargeBiMonthly"))) for slab in _list(fixed.get("threePhase"))),
            fixed_charge_bi_monthly=json_number(data.get("fixedChargeBiMonthly")),
            meter_rent=(MeterRent(json_number(meter.get("singlePhaseBiMonthly")), json_number(meter.get("threePhaseBiMonthly"))) if isinstance(meter, Mapping) else None),
            meter_rent_bi_monthly=json_number(data.get("meterRentBiMonthly")),
            electricity_duty_pct=json_number(data.get("electricityDutyPct")),
            yield_assumption=(YieldAssumption(json_number(daily) if _is_json_number(daily) else None, yield_data.get("unit"), yield_data.get("source")) if isinstance(yield_data, Mapping) else None),
            sizing=(SizingRule(json_number(sizing.get("coverageTarget")), sizing.get("note")) if isinstance(sizing, Mapping) else None),
        )


def _list(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _is_json_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


@dataclass(frozen=True)
class EnergyConfig:
    """``energy-config.json``: ``configVersion``, ``defaultRegion`` and the regions by key."""

    config_version: str | None
    default_region: str | None
    regions: Mapping[str, RegionConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EnergyConfig:
        regions = data.get("regions") if isinstance(data.get("regions"), Mapping) else {}
        return cls(
            config_version=data.get("configVersion"),
            default_region=data.get("defaultRegion"),
            regions={key: RegionConfig.from_json(value) for key, value in regions.items() if isinstance(value, Mapping)},
        )


def validate_energy_config(data: Any) -> Mapping[str, Any]:
    """``validateEnergyConfig`` on the raw JSON: an object with a ``regions`` object and a ``configVersion`` string."""
    if not isinstance(data, Mapping):
        raise ValueError("Energy config must be a non-null object")
    if not isinstance(data.get("regions"), Mapping):
        raise ValueError('Energy config must have a "regions" object')
    if not data.get("configVersion") or not isinstance(data.get("configVersion"), str):
        raise ValueError('Energy config must have a "configVersion" string')
    return data


def list_regions(config: EnergyConfig) -> list[dict[str, Any]]:
    """``listRegions``: region id, name, DISCOM, tariff name and version."""
    return [
        {"regionId": key, "regionName": region.region_name, "discom": region.discom, "tariffName": region.tariff_name, "tariffVersion": region.tariff_version} for key, region in config.regions.items()
    ]


# ---- units → bill (Decimal) ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BillBreakdown:
    """A bi-monthly bill: whole-rupee energy charge and duty, fixed charge, meter rent and their total.

    ``energy_charge_exact`` (the unrounded energy charge the duty is computed on) is kept for audit; it is not part of
    the JavaScript result object.
    """

    energy_charge: Decimal
    fixed_charge: Decimal
    duty: Decimal
    meter_rent: Decimal
    total: Decimal
    bi_monthly_units: Decimal | int
    monthly_units: Decimal
    billing_category: BillingCategory
    phase: str
    energy_charge_exact: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "energyCharge": self.energy_charge,
            "fixedCharge": self.fixed_charge,
            "duty": self.duty,
            "meterRent": self.meter_rent,
            "total": self.total,
            "biMonthlyUnits": self.bi_monthly_units,
            "monthlyUnits": self.monthly_units,
            "billingCategory": str(self.billing_category),
            "phase": self.phase,
        }


def _first_match(units: Decimal, bands: tuple[Any, ...], attribute: str) -> Decimal:
    for band in bands:
        if band.up_to_units is None or units <= band.up_to_units:
            return getattr(band, attribute)
    return getattr(bands[-1], attribute)


def _fixed_charge(units: Decimal, region: RegionConfig, phase: str) -> Decimal:
    slabs = region.fixed_charge_three_phase if phase == Phase.THREE else region.fixed_charge_single_phase
    if slabs:
        return _first_match(units, slabs, "charge_bi_monthly")
    return _default(region.fixed_charge_bi_monthly, DEFAULT_FIXED_CHARGE_BI_MONTHLY)


def _meter_rent(region: RegionConfig, phase: str) -> Decimal:
    if region.meter_rent is not None:
        if phase == Phase.THREE:
            return _default(region.meter_rent.three_phase_bi_monthly, DEFAULT_METER_RENT_THREE)
        return _default(region.meter_rent.single_phase_bi_monthly, DEFAULT_METER_RENT_SINGLE)
    return _default(region.meter_rent_bi_monthly, DEFAULT_METER_RENT_FLAT)


def _default(value: Decimal | None, fallback: Decimal) -> Decimal:
    return fallback if value is None else value


@exact
def units_to_bill(bi_monthly_units: Decimal | int, region: RegionConfig, phase: str = Phase.SINGLE) -> BillBreakdown:
    """``unitsToBill``: the bi-monthly bill of ``bi_monthly_units`` (``phase`` ``'three'`` selects the three-phase
    fixed charge and meter rent; anything else is single phase and is echoed as given)."""
    units = to_decimal(bi_monthly_units, field="bi_monthly_units")
    threshold = _default(region.non_telescopic_threshold, DEFAULT_NON_TELESCOPIC_THRESHOLD)
    energy = ZERO
    if units <= threshold:
        category = BillingCategory.TELESCOPIC
        remaining = units
        previous = ZERO
        for slab in region.tariff_slabs:
            upper = slab.up_to_units if slab.up_to_units is not None else ZERO
            slab_units = min(remaining, upper - previous)
            if slab_units <= 0:
                break
            energy += slab_units * slab.rate_per_unit
            remaining -= slab_units
            previous = upper
    else:
        category = BillingCategory.NON_TELESCOPIC
        rate = _first_match(units, region.non_telescopic_slabs, "rate_per_unit") if region.non_telescopic_slabs else _default(region.non_telescopic_rate, DEFAULT_NON_TELESCOPIC_RATE)
        energy = units * rate
    fixed = _fixed_charge(units, region, phase)
    meter = _meter_rent(region, phase)
    duty_pct = _default(region.electricity_duty_pct, DEFAULT_DUTY_PCT)
    duty = js_round(energy * (duty_pct / 100))  # on the UNROUNDED energy charge
    energy_charge = js_round(energy)
    return BillBreakdown(
        energy_charge=energy_charge,
        fixed_charge=fixed,
        duty=duty,
        meter_rent=meter,
        total=energy_charge + fixed + duty + meter,
        bi_monthly_units=bi_monthly_units,
        monthly_units=js_round(units / 2),
        billing_category=category,
        phase=str(phase),
        energy_charge_exact=energy,
    )


# ---- bill → units (binary64 replica of the JavaScript search) ------------------------------------------------------


@dataclass(frozen=True)
class _Binary64Region:
    """The region's numbers as IEEE-754 doubles — what ``JSON.parse`` gives the JavaScript for the same decimal text."""

    threshold: float
    slabs: tuple[tuple[float | None, float], ...]
    bands: tuple[tuple[float | None, float], ...]
    flat_rate: float
    fixed: tuple[tuple[float | None, float], ...]
    flat_fixed: float
    meter: float
    duty_pct: float

    @classmethod
    def of(cls, region: RegionConfig, phase: str) -> _Binary64Region:
        def f(value: Decimal | None) -> float | None:
            return None if value is None else float(value)

        slabs = region.fixed_charge_three_phase if phase == Phase.THREE else region.fixed_charge_single_phase
        return cls(
            threshold=float(_default(region.non_telescopic_threshold, DEFAULT_NON_TELESCOPIC_THRESHOLD)),
            slabs=tuple((f(slab.up_to_units), float(slab.rate_per_unit)) for slab in region.tariff_slabs),
            bands=tuple((f(band.up_to_units), float(band.rate_per_unit)) for band in region.non_telescopic_slabs),
            flat_rate=float(_default(region.non_telescopic_rate, DEFAULT_NON_TELESCOPIC_RATE)),
            fixed=tuple((f(slab.up_to_units), float(slab.charge_bi_monthly)) for slab in slabs),
            flat_fixed=float(_default(region.fixed_charge_bi_monthly, DEFAULT_FIXED_CHARGE_BI_MONTHLY)),
            meter=float(_meter_rent(region, phase)),
            duty_pct=float(_default(region.electricity_duty_pct, DEFAULT_DUTY_PCT)),
        )


def _binary64_math_round(value: float) -> float:
    """``Math.round`` on a double: nearest integer, ties toward +∞ (exact: ``x − floor(x)`` is exact for x ≥ 0)."""
    if value < 0:
        return float(js_round(Decimal(value)))
    whole = math.floor(value)
    return float(whole + 1) if value - whole >= 0.5 else float(whole)


def _binary64_first(units: float, bands: tuple[tuple[float | None, float], ...]) -> float:
    for upper, value in bands:
        if upper is None or units <= upper:
            return value
    return bands[-1][1]


def _binary64_energy(units: float, region: _Binary64Region) -> float:
    """The unrounded energy charge of ``units`` in binary64, operation for operation as the JavaScript."""
    energy = 0.0
    if units <= region.threshold:
        remaining = units
        previous = 0.0
        for upper, rate in region.slabs:
            upper_value = upper if upper is not None else 0.0
            slab_units = min(remaining, upper_value - previous)
            if slab_units <= 0:
                break
            energy += slab_units * rate
            remaining -= slab_units
            previous = upper_value
        return energy
    return units * (_binary64_first(units, region.bands) if region.bands else region.flat_rate)


def _binary64_bill_total(units: float, region: _Binary64Region) -> float:
    """``unitsToBill(units).total`` evaluated in binary64 exactly as the JavaScript evaluates it."""
    energy = _binary64_energy(units, region)
    fixed = _binary64_first(units, region.fixed) if region.fixed else region.flat_fixed
    duty = _binary64_math_round(energy * (region.duty_pct / 100))
    return _binary64_math_round(energy) + fixed + duty + region.meter


def bill_to_units(bi_monthly_bill: Decimal | int, region: RegionConfig, phase: str = Phase.SINGLE) -> int:
    """``billToUnits``: the bi-monthly units whose bill reaches ``bi_monthly_bill`` (0–3000, whole units).

    A bit-for-bit binary64 replica of the JavaScript bisection (module docstring): 50 halvings of [0, 3000], moving
    the lower end while the bill is below the target, then ``Math.round`` of the midpoint.
    """
    target = float(to_decimal(bi_monthly_bill, field="bi_monthly_bill"))
    doubles = _Binary64Region.of(region, phase)
    low = 0.0
    high = float(SEARCH_UPPER_UNITS)
    for _ in range(SEARCH_STEPS):
        middle = (low + high) / 2
        if _binary64_bill_total(middle, doubles) < target:
            low = middle
        else:
            high = middle
    return int(_binary64_math_round((low + high) / 2))


# ---- the energy profile --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EnergyInputs:
    """The customer's bill: amount (₹), ``monthly``/``bimonthly`` cycle, connection phase, optional quoted size (kW).

    ``bill_amount`` may be a Decimal, an int or a numeric string (anything else, zero or negative is
    ``INVALID_BILL_AMOUNT``). A ``system_size_kw`` that is not a positive number means "size it for me".
    """

    bill_amount: Any
    billing_cycle: str | None = BillingCycle.MONTHLY
    phase: str | None = Phase.SINGLE
    system_size_kw: Decimal | int | None = None

    def __post_init__(self) -> None:
        for name in ("bill_amount", "system_size_kw"):
            if isinstance(getattr(self, name), float):
                raise TypeError(f"{name}: float is not accepted in an engine; pass a Decimal, an int or a numeric string")


@dataclass(frozen=True)
class EnergyBlocked:
    """A BLOCKED energy result (no region, no yield, no tariff, or no positive bill)."""

    error: EnergyError
    reason: str
    region_id: str | None
    available: bool = False
    status: str = ENERGY_STATUS_BLOCKED

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": False,
            "source": ENERGY_SOURCE,
            "status": ENERGY_STATUS_BLOCKED,
            "error": str(self.error),
            "reason": self.reason,
            "regionId": self.region_id,
            "engineVersion": ENERGY_ENGINE_VERSION,
        }


@dataclass(frozen=True)
class EnergyProfile:
    """``energyProfileResult``: consumption, bill breakdown, effective tariff, sizing and generation estimates."""

    region_id: str
    region_name: str | None
    discom: str | None
    tariff_name: str | None
    tariff_version: str | None
    config_version: str | None
    daily_gen_per_kw: Decimal
    yield_unit: str | None
    yield_source: str | None
    coverage_target: Decimal
    sizing_note: str
    monthly_consumption: Decimal
    annual_consumption: Decimal
    bi_monthly_units: int
    bill_breakdown: BillBreakdown | None
    average_tariff_rate: Decimal
    phase: str
    exact_system_size_kw: Decimal
    recommended_system_size_kw: Decimal
    daily_generation_low: Decimal
    daily_generation_high: Decimal
    monthly_generation: Decimal
    annual_generation: Decimal
    monthly_kseb_value_low: Decimal
    monthly_kseb_value_high: Decimal
    home_uses_units_per_day: Decimal
    surplus_exported_low: Decimal
    surplus_exported_high: Decimal
    available: bool = True
    status: str = ENERGY_STATUS_LIVE

    def as_dict(self) -> dict[str, Any]:
        """The JavaScript result object (the quotation payload contract), key for key."""
        return {
            "available": True,
            "source": ENERGY_SOURCE,
            "status": ENERGY_STATUS_LIVE,
            "engineVersion": ENERGY_ENGINE_VERSION,
            "regionId": self.region_id,
            "regionName": self.region_name,
            "discom": self.discom,
            "tariffName": self.tariff_name,
            "tariffVersion": self.tariff_version,
            "configVersion": self.config_version,
            "yieldAssumption": {"dailyGenPerKw": self.daily_gen_per_kw, "unit": self.yield_unit, "source": self.yield_source},
            "sizingAssumption": {"coverageTarget": self.coverage_target, "note": self.sizing_note},
            "monthlyConsumption": self.monthly_consumption,
            "monthlyConsumptionUnit": "kWh",
            "annualConsumption": self.annual_consumption,
            "annualConsumptionUnit": "kWh",
            "biMonthlyUnits": self.bi_monthly_units,
            "billBreakdown": self.bill_breakdown.as_dict() if self.bill_breakdown is not None else None,
            "averageTariffRate": self.average_tariff_rate,
            "averageTariffRateUnit": "₹/kWh",
            "averageTariffRateNote": AVERAGE_TARIFF_RATE_NOTE,
            "phase": self.phase,
            "exactSystemSizeKw": self.exact_system_size_kw,
            "recommendedSystemSizeKw": self.recommended_system_size_kw,
            "dailyGenerationLow": self.daily_generation_low,
            "dailyGenerationHigh": self.daily_generation_high,
            "dailyGenerationUnit": "kWh",
            "monthlyGeneration": self.monthly_generation,
            "monthlyGenerationUnit": "kWh",
            "annualGeneration": self.annual_generation,
            "annualGenerationUnit": "kWh",
            "monthlyKsebValueLow": self.monthly_kseb_value_low,
            "monthlyKsebValueHigh": self.monthly_kseb_value_high,
            "monthlyKsebValueUnit": "₹",
            "homeUsesUnitsPerDay": self.home_uses_units_per_day,
            "homeUsesUnitsPerDayUnit": "kWh",
            "surplusExportedLow": self.surplus_exported_low,
            "surplusExportedHigh": self.surplus_exported_high,
            "surplusExportedUnit": "kWh/day",
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> EnergyProfile:
        """An available result back from its payload form (a frozen quotation payload), e.g. to recompute savings."""

        def number(key: str, source: Mapping[str, Any] = data) -> Decimal | None:
            return json_number(source.get(key))

        breakdown = data.get("billBreakdown")
        yield_data = data.get("yieldAssumption") or {}
        sizing = data.get("sizingAssumption") or {}
        return cls(
            region_id=data.get("regionId"),
            region_name=data.get("regionName"),
            discom=data.get("discom"),
            tariff_name=data.get("tariffName"),
            tariff_version=data.get("tariffVersion"),
            config_version=data.get("configVersion"),
            daily_gen_per_kw=number("dailyGenPerKw", yield_data),
            yield_unit=yield_data.get("unit"),
            yield_source=yield_data.get("source"),
            coverage_target=number("coverageTarget", sizing),
            sizing_note=sizing.get("note"),
            monthly_consumption=number("monthlyConsumption"),
            annual_consumption=number("annualConsumption"),
            bi_monthly_units=data.get("biMonthlyUnits"),
            bill_breakdown=(
                BillBreakdown(
                    energy_charge=number("energyCharge", breakdown),
                    fixed_charge=number("fixedCharge", breakdown),
                    duty=number("duty", breakdown),
                    meter_rent=number("meterRent", breakdown),
                    total=number("total", breakdown),
                    bi_monthly_units=number("biMonthlyUnits", breakdown),
                    monthly_units=number("monthlyUnits", breakdown),
                    billing_category=BillingCategory(breakdown.get("billingCategory")),
                    phase=breakdown.get("phase"),
                )
                if isinstance(breakdown, Mapping)
                else None
            ),
            average_tariff_rate=number("averageTariffRate"),
            phase=data.get("phase"),
            exact_system_size_kw=number("exactSystemSizeKw"),
            recommended_system_size_kw=number("recommendedSystemSizeKw"),
            daily_generation_low=number("dailyGenerationLow"),
            daily_generation_high=number("dailyGenerationHigh"),
            monthly_generation=number("monthlyGeneration"),
            annual_generation=number("annualGeneration"),
            monthly_kseb_value_low=number("monthlyKsebValueLow"),
            monthly_kseb_value_high=number("monthlyKsebValueHigh"),
            home_uses_units_per_day=number("homeUsesUnitsPerDay"),
            surplus_exported_low=number("surplusExportedLow"),
            surplus_exported_high=number("surplusExportedHigh"),
        )


def _parse_bill_amount(value: Any) -> Decimal:
    """``parseFloat(billAmount) || 0`` for typed inputs: a Decimal, an int or a numeric string; anything else is 0."""
    if isinstance(value, bool) or value is None:
        return ZERO
    try:
        return to_decimal(value, field="bill_amount")
    except (TypeError, ValueError):
        return ZERO


@exact
def calculate_energy_profile(inputs: EnergyInputs, config: EnergyConfig, region_id: str | None = None, *, fix_three_phase_tariff: bool = True) -> EnergyProfile | EnergyBlocked:
    """``calculateEnergyProfile``: bill → consumption → size → generation (spec §2).

    ``fix_three_phase_tariff`` is decision D-8 (see :func:`resolve_phase`); ``False`` reproduces the JavaScript.
    """
    resolved_region = region_id or config.default_region
    region = config.regions.get(resolved_region) if resolved_region is not None else None
    region_text = resolved_region if resolved_region is not None else "undefined"
    if region is None:
        return EnergyBlocked(
            EnergyError.NO_REGION_CONFIG,
            f'No energy configuration for region "{region_text}". Cannot silently use another region\'s tariff or yield.',
            resolved_region,
        )
    if region.yield_assumption is None or region.yield_assumption.daily_gen_per_kw is None:
        return EnergyBlocked(EnergyError.NO_YIELD_CONFIG, f'No yield configuration for region "{region_text}".', resolved_region)
    if not region.tariff_slabs:
        return EnergyBlocked(EnergyError.NO_TARIFF_CONFIG, f'No tariff configuration for region "{region_text}".', resolved_region)

    phase = resolve_phase(inputs.phase, fix_three_phase_tariff=fix_three_phase_tariff)
    amount = _parse_bill_amount(inputs.bill_amount)
    if amount <= 0:
        return EnergyBlocked(EnergyError.INVALID_BILL_AMOUNT, "Bill amount must be a positive number.", resolved_region)

    monthly_bill = amount / 2 if inputs.billing_cycle == BillingCycle.BIMONTHLY else amount
    bi_monthly_units = bill_to_units(monthly_bill * 2, region, phase)
    breakdown = units_to_bill(bi_monthly_units, region, phase)
    monthly_units = js_round(Decimal(bi_monthly_units) / 2)

    daily_gen_per_kw = region.yield_assumption.daily_gen_per_kw
    monthly_gen_per_kw = js_round(daily_gen_per_kw * 30)
    coverage = region.sizing.coverage_target if region.sizing is not None and region.sizing.coverage_target is not None else DEFAULT_COVERAGE_TARGET
    size = inputs.system_size_kw
    if is_number(size) and size > 0:
        recommended = Decimal(size)
    else:
        # the smallest whole kW covering ≥ coverage of consumption (not a catalog size — spec §21.2)
        recommended = max(Decimal(1), (monthly_units * coverage / monthly_gen_per_kw).to_integral_value(rounding=ROUND_CEILING))

    daily_generation = recommended * daily_gen_per_kw
    monthly_generation = recommended * monthly_gen_per_kw
    daily_low = round_places(daily_generation * Decimal("0.9"), 1)
    daily_high = round_places(daily_generation * Decimal("1.1"), 1)
    # EFFECTIVE bill rate (total bill / units, incl. fixed charge, duty and meter rent) — the Savings Engine relies on it.
    average_rate = js_round(breakdown.total * 100 / (2 * monthly_units)) / 100 if monthly_units > 0 else ZERO
    home_uses = round_places(monthly_units / 30, 1) if monthly_units > 0 else ZERO
    sizing_note = region.sizing.note if region.sizing is not None and region.sizing.note is not None else f"System sized to cover ≥{canonical(js_round(coverage * 100))}% of consumption"
    return EnergyProfile(
        region_id=resolved_region,
        region_name=region.region_name,
        discom=region.discom,
        tariff_name=region.tariff_name,
        tariff_version=region.tariff_version,
        config_version=config.config_version,
        daily_gen_per_kw=daily_gen_per_kw,
        yield_unit=region.yield_assumption.unit,
        yield_source=region.yield_assumption.source,
        coverage_target=coverage,
        sizing_note=sizing_note,
        monthly_consumption=monthly_units,
        annual_consumption=monthly_units * 12,
        bi_monthly_units=bi_monthly_units,
        bill_breakdown=breakdown,
        average_tariff_rate=average_rate,
        phase=str(phase),
        exact_system_size_kw=round_places(monthly_units / monthly_gen_per_kw, 1),
        recommended_system_size_kw=recommended,
        daily_generation_low=daily_low,
        daily_generation_high=daily_high,
        monthly_generation=monthly_generation,
        annual_generation=monthly_generation * 12,
        monthly_kseb_value_low=js_round(daily_low * 30 * average_rate),
        monthly_kseb_value_high=js_round(daily_high * 30 * average_rate),
        home_uses_units_per_day=home_uses,
        surplus_exported_low=max(ZERO, round_places(daily_low - home_uses, 1)),
        surplus_exported_high=max(ZERO, round_places(daily_high - home_uses, 1)),
    )
