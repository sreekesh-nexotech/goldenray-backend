"""Recommend a system size from an electricity bill (Golden-Ray-BOM ``billCalc``).

A sales person usually has the customer's bill, not a kW figure. This turns one into the
other: bill → bi-monthly units → kW of generation needed → the nearest size the template
actually offers.

The tariff arithmetic is **not** reimplemented here. ``engines.energy`` already holds a
bit-exact port of the same KSEB LT-1A slabs and the same 50-halving bisection the source
uses, driven by ``pricing_cost_config["energy.config"]``, so this module does the sizing
on top of it. That matters because the slabs change by tariff order: one engine means one
place to update.

Deliberately separate from ``calculators/``: the public calculators answer from a
``BillRangeSize`` lookup table, which is a different method and gives different numbers.
Sales staff need the figure the BOM tool gives them, so this is its own staff endpoint
rather than a reuse that would quietly shift their recommendation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from core.errors import DomainError
from engines.energy import Phase, RegionConfig, bill_to_units, units_to_bill

#: The source's own assumptions (App.jsx:965-1025), used when the region's config is
#: silent. 4 units per kW per day is the Kerala yield; a size is accepted once it covers
#: 85 % of consumption, because the last stretch of a bill is the least worth paying for.
DEFAULT_DAILY_GEN_PER_KW = Decimal("4.0")
DEFAULT_COVERAGE_TARGET = Decimal("0.85")
#: Cost per kW used only for the payback estimate shown beside the recommendation.
DEFAULT_COST_PER_KW = Decimal("55000")
VALID_FREQUENCY = ("monthly", "bimonthly")
VALID_PHASE = ("1P", "3P")


@dataclass(frozen=True)
class SizeOption:
    """A size the template offers, with the phase it needs."""

    key: str
    kw: Decimal
    three_phase: bool


def _region(config: dict | None) -> RegionConfig:
    from engines.energy import EnergyConfig

    if not config:
        # No configured tariff is a configuration problem, not a quietly wrong answer.
        raise DomainError(
            "bill_sizing_not_configured",
            "The energy configuration (energy.config) is not set, so a bill cannot be converted to units.",
            status=503,
            errors={"cost_config": ["energy.config"]},
        )
    energy = EnergyConfig.from_json(config)
    region = energy.regions.get(energy.default_region or "") or next(iter(energy.regions.values()), None)
    if region is None:
        raise DomainError(
            "bill_sizing_not_configured",
            "The energy configuration defines no region.",
            status=503,
            errors={"cost_config": ["energy.config.regions"]},
        )
    return region


def recommend(*, bill: Decimal, frequency: str, house_phase: str, sizes: list[SizeOption], config: dict | None) -> dict:
    """What to install for this bill, and why.

    ``bill`` is what the customer pays per ``frequency``. KSEB bills bi-monthly, so a
    monthly figure is doubled before it reaches the tariff — getting that backwards
    halves or doubles the recommendation, which is why the frequency is explicit rather
    than inferred.
    """
    if frequency not in VALID_FREQUENCY:
        raise DomainError("validation_error", "frequency must be monthly | bimonthly", status=400, errors={"frequency": ["monthly | bimonthly"]})
    if house_phase not in VALID_PHASE:
        raise DomainError("validation_error", "house_phase must be 1P | 3P", status=400, errors={"house_phase": ["1P | 3P"]})
    if bill <= 0:
        raise DomainError("validation_error", "bill must be greater than zero", status=400, errors={"bill": ["Must be greater than zero."]})

    region = _region(config)
    phase = Phase.THREE if house_phase == "3P" else Phase.SINGLE
    bi_monthly_bill = bill if frequency == "bimonthly" else bill * 2

    bi_monthly_units = bill_to_units(bi_monthly_bill, region, phase)
    monthly_units = round(bi_monthly_units / 2)

    yield_per_kw = (region.yield_assumption.daily_gen_per_kw if region.yield_assumption else None) or DEFAULT_DAILY_GEN_PER_KW
    monthly_gen_per_kw = Decimal(round(yield_per_kw * 30))
    exact_kw = (Decimal(monthly_units) / monthly_gen_per_kw) if monthly_gen_per_kw else Decimal(0)

    coverage = (region.sizing.coverage_target if region.sizing else None) or DEFAULT_COVERAGE_TARGET
    recommended = _pick(sizes, exact_kw, coverage, house_phase)

    # What the customer would actually save: a system cannot offset more than the house
    # consumes, so generation is capped at consumption before it becomes money.
    breakdown = units_to_bill(bi_monthly_units, region, phase)
    bi_monthly_total = Decimal(breakdown.total)
    average_rate = (bi_monthly_total / 2 / Decimal(monthly_units)) if monthly_units else Decimal(0)
    generated = (recommended.kw * monthly_gen_per_kw) if recommended else Decimal(0)
    offset_units = min(generated, Decimal(monthly_units))
    monthly_savings = round(offset_units * average_rate)
    annual_savings = monthly_savings * 12
    payback_years = (
        round(float(recommended.kw * DEFAULT_COST_PER_KW) / annual_savings, 1) if recommended and annual_savings else None
    )

    return {
        "bi_monthly_bill": float(bi_monthly_bill),
        "bi_monthly_units": bi_monthly_units,
        "monthly_units": monthly_units,
        "exact_kw": round(float(exact_kw), 1),
        "recommended_size": recommended.key if recommended else None,
        "recommended_kw": float(recommended.kw) if recommended else None,
        # A single-phase house needing more than 5 kW has to upgrade its supply first, and
        # that is a KSEB application the customer has to make -- worth saying up front
        # rather than discovering at installation (App.jsx:1015).
        "phase_upgrade_needed": house_phase == "1P" and exact_kw > 5,
        "monthly_savings": monthly_savings,
        "annual_savings": annual_savings,
        "payback_years": payback_years,
        "average_unit_rate": round(float(average_rate), 2),
        "bill_breakdown": {
            "energy_charge": float(breakdown.energy_charge),
            "fixed_charge": float(breakdown.fixed_charge),
            "duty": float(breakdown.duty),
            "meter_rent": float(breakdown.meter_rent),
            "total": float(breakdown.total),
        },
    }


def _pick(sizes: list[SizeOption], exact_kw: Decimal, coverage: Decimal, house_phase: str) -> SizeOption | None:
    """The smallest offered size that covers enough of the consumption.

    Smallest-that-fits rather than nearest: over-sizing sells capacity the customer
    exports at a lower rate than they buy, so the bias is deliberately downward. If
    nothing reaches the target the largest available size is the honest answer — it is
    what they can have, even though it will not cover the whole bill.
    """
    usable = [size for size in sizes if size.three_phase == (house_phase == "3P") or not size.three_phase]
    if house_phase == "3P":
        # A three-phase supply can carry a single-phase system, but the source does not
        # offer the smallest size there -- it is not worth a 3P connection.
        usable = [size for size in sizes if size.three_phase or size.kw > 3]
    if not usable:
        return None
    ordered = sorted(usable, key=lambda size: size.kw)
    target = exact_kw * coverage
    for size in ordered:
        if size.kw >= target:
            return size
    return ordered[-1]


def size_options(sizes: list, three_phase_keys: list) -> list[SizeOption]:
    """Template sizes as options, with ``5sp``/``5tp`` distinguished by phase, not kW."""
    options = []
    for entry in sizes or []:
        key = entry.get("key") if isinstance(entry, dict) else str(entry)
        if not key:
            continue
        try:
            kw = Decimal(str(math.floor(float(key.replace("sp", "").replace("tp", "")) * 100) / 100))
        except (TypeError, ValueError):
            continue
        options.append(SizeOption(key=key, kw=kw, three_phase=key in (three_phase_keys or [])))
    return options
