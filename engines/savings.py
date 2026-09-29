"""Savings engine: first-year savings as a KSEB bill difference, payback, and the config-gated lifetime projection.

Port of Flarize ``src/lib/savingsEngine.js`` (version ``'2.0.0'``, spec §3), a protected formula (spec §19):

* self-consumption model FULL_OFFSET — generation offsets grid purchase, capped at consumption;
* V2 (a region tariff is given): ``savings = bill(consumption) − bill(consumption − generation)``, each bill from
  :func:`engines.energy.units_to_bill` and halved to a month; V1 fallback (no region): ``generation × averageTariffRate``;
* payback on the **gross** investment (``customerTotalIncludingGST``, subsidy not deducted — D-8 keeps the Flarize
  basis): ``ceil(investment / annual savings × 12)`` months;
* the 25-year projection stays ``None`` until the business approves tariff escalation and degradation.

Exact Decimal arithmetic, rounded with ``Math.round`` where the JavaScript rounds (:func:`engines.money.js_round`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any, Mapping

from engines.energy import BillBreakdown, EnergyBlocked, EnergyProfile, Phase, RegionConfig, units_to_bill
from engines.money import HUNDRED, ONE, ZERO, canonical, exact, is_number, js_round, json_number, round_places, to_decimal

SAVINGS_ENGINE_VERSION = "2.0.0"
SAVINGS_SOURCE = "SAVINGS_ENGINE"
SAVINGS_STATUS_LIVE = "LIVE"


class SavingsError(StrEnum):
    MISSING_ENERGY_RESULT = "MISSING_ENERGY_RESULT"
    MISSING_TARIFF = "MISSING_TARIFF"
    MISSING_INVESTMENT = "MISSING_INVESTMENT"
    ZERO_GENERATION = "ZERO_GENERATION"
    ZERO_CONSUMPTION = "ZERO_CONSUMPTION"
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_REGION_CONFIG = "MISSING_REGION_CONFIG"


class SelfConsumptionModel(StrEnum):
    FULL_OFFSET = "FULL_OFFSET"


BILL_DIFFERENCE_BASIS = "BILL_DIFFERENCE — savings = KSEB_bill(consumption) − KSEB_bill(consumption − generation)"
FIRST_YEAR_BASIS = "FIRST_YEAR — generation × averageTariffRate (V1 fallback, no regionConfig)"


@dataclass(frozen=True)
class SavingsConfig:
    """``savings-config.json``. The projection needs both escalation bounds and the degradation rate as percentages
    in [0, 100) and a positive whole number of years; any ``None`` keeps it off (the approved state today)."""

    config_version: str | None = None
    self_consumption_model: str | None = SelfConsumptionModel.FULL_OFFSET
    lifetime_years: Decimal | int | None = None
    degradation_rate_per_year: Decimal | None = None
    tariff_escalation_low: Decimal | None = None
    tariff_escalation_high: Decimal | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> SavingsConfig:
        data = data or {}
        years = data.get("lifetimeYears")
        return cls(
            config_version=data.get("configVersion"),
            self_consumption_model=data.get("selfConsumptionModel"),
            lifetime_years=json_number(years) if not isinstance(years, bool) and isinstance(years, (int, float, Decimal)) else None,
            degradation_rate_per_year=json_number(data.get("degradationRatePerYear")),
            tariff_escalation_low=json_number(data.get("tariffEscalationLow")),
            tariff_escalation_high=json_number(data.get("tariffEscalationHigh")),
        )


def validate_savings_config(data: Any) -> tuple[bool, list[str]]:
    """``validateSavingsConfig`` on the raw JSON: ``(valid, errors)``."""
    if not isinstance(data, Mapping):
        return False, ["Config must be a non-null object"]
    errors = []
    model = data.get("selfConsumptionModel")
    if not model:
        errors.append("Missing selfConsumptionModel")
    elif model != SelfConsumptionModel.FULL_OFFSET:
        errors.append(f"Unsupported selfConsumptionModel: {model}")
    years = data.get("lifetimeYears")
    if years is not None and (isinstance(years, bool) or not isinstance(years, (int, float, Decimal)) or years <= 0):
        errors.append("lifetimeYears must be a positive number or null")
    return not errors, errors


# ---- lifetime projection -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphPoint:
    year: int
    without_solar_cumulative: Decimal
    with_solar_cumulative: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {"year": self.year, "withoutSolarCumulative": self.without_solar_cumulative, "withSolarCumulative": self.with_solar_cumulative}


@dataclass(frozen=True)
class LifetimeProjection:
    available: bool = False
    lifetime_years: int | None = None
    degradation_rate_per_year: Decimal | None = None
    tariff_escalation_low: Decimal | None = None
    tariff_escalation_high: Decimal | None = None
    lifetime_kseb_spend: Decimal | None = None
    lifetime_savings_amount: Decimal | None = None
    graph_points: tuple[GraphPoint, ...] | None = None


NO_PROJECTION = LifetimeProjection()


def _percent(value: Any) -> Decimal | None:
    """``finitePct``: a number in [0, 100), else ``None``."""
    return Decimal(value) if is_number(value) and 0 <= value < 100 else None


@exact
def project_lifetime(
    config: SavingsConfig,
    current_monthly_bill: Decimal | int | None,
    post_solar_bill_low: Decimal | int | None,
    post_solar_bill_high: Decimal | int | None,
    monthly_savings: Decimal | int | None,
    customer_total_including_gst: Decimal | int | None = None,
) -> LifetimeProjection:
    """``projectLifetime``: cumulative KSEB spend without solar vs. system cost + residual bills, per year.

    KSEB bill in year y = current × 12 × (1 + esc)^(y−1) with esc the midpoint of the escalation bounds; the residual
    bill adds the savings lost to degradation (``min(1, deg × (y−1))`` of the offset).
    """
    esc_low = _percent(config.tariff_escalation_low)
    esc_high = _percent(config.tariff_escalation_high)
    degradation = _percent(config.degradation_rate_per_year)
    years = config.lifetime_years
    years = int(years) if is_number(years) and years == int(years) and years > 0 else None
    if esc_low is None or esc_high is None or degradation is None or years is None:
        return NO_PROJECTION
    if not (is_number(current_monthly_bill) and current_monthly_bill > 0) or post_solar_bill_low is None or post_solar_bill_high is None:
        return NO_PROJECTION
    if not (is_number(monthly_savings) and monthly_savings >= 0):
        return NO_PROJECTION
    escalation = (esc_low + esc_high) / 2 / HUNDRED
    degradation_rate = degradation / HUNDRED
    post_mid = (Decimal(post_solar_bill_low) + Decimal(post_solar_bill_high)) / 2
    investment = Decimal(customer_total_including_gst) if is_number(customer_total_including_gst) and customer_total_including_gst > 0 else ZERO
    cumulative_kseb = ZERO
    cumulative_solar = investment
    points = []
    for year in range(1, years + 1):
        growth = (ONE + escalation) ** (year - 1)
        kseb_year = current_monthly_bill * 12 * growth
        lost_savings = monthly_savings * 12 * growth * min(ONE, degradation_rate * (year - 1))
        cumulative_kseb += kseb_year
        cumulative_solar += post_mid * 12 * growth + lost_savings
        points.append(GraphPoint(year, js_round(cumulative_kseb), js_round(cumulative_solar)))
    return LifetimeProjection(
        available=True,
        lifetime_years=years,
        degradation_rate_per_year=degradation,
        tariff_escalation_low=esc_low,
        tariff_escalation_high=esc_high,
        lifetime_kseb_spend=js_round(cumulative_kseb),
        lifetime_savings_amount=max(ZERO, js_round(cumulative_kseb - cumulative_solar)),
        graph_points=tuple(points),
    )


# ---- results -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonthlyBillBreakdown:
    """A bi-monthly bill halved to a month, each part rounded."""

    energy_charge: Decimal
    fixed_charge: Decimal
    duty: Decimal
    meter_rent: Decimal
    billing_category: str

    @classmethod
    def of(cls, bill: BillBreakdown) -> MonthlyBillBreakdown:
        return cls(js_round(bill.energy_charge / 2), js_round(bill.fixed_charge / 2), js_round(bill.duty / 2), js_round(bill.meter_rent / 2), str(bill.billing_category))

    def as_dict(self) -> dict[str, Any]:
        return {"energyCharge": self.energy_charge, "fixedCharge": self.fixed_charge, "duty": self.duty, "meterRent": self.meter_rent, "billingCategory": self.billing_category}


@dataclass(frozen=True)
class SavingsBlocked:
    status: SavingsError
    reason: str
    available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"source": SAVINGS_SOURCE, "status": str(self.status), "available": False, "reason": self.reason, "engineVersion": SAVINGS_ENGINE_VERSION}


@dataclass(frozen=True)
class SavingsResult:
    system_size_kw: Decimal | None
    daily_generation_units: Decimal | None
    annual_output_kwh: Decimal | None
    monthly_savings_low: Decimal
    monthly_savings_high: Decimal
    monthly_savings: Decimal
    daily_savings: Decimal
    annual_savings_amount: Decimal
    return_period_months: Decimal | None
    post_solar_bill_low: Decimal
    post_solar_bill_high: Decimal
    current_monthly_bill: Decimal
    current_bill_breakdown: MonthlyBillBreakdown | None
    post_solar_bill_breakdown: MonthlyBillBreakdown | None
    projection: LifetimeProjection
    savings_basis: str
    payback_basis: str
    available: bool = True
    status: str = SAVINGS_STATUS_LIVE

    def as_dict(self) -> dict[str, Any]:
        """The JavaScript result object (the quotation payload contract), key for key."""
        projection = self.projection
        return {
            "source": SAVINGS_SOURCE,
            "status": SAVINGS_STATUS_LIVE,
            "available": True,
            "engineVersion": SAVINGS_ENGINE_VERSION,
            "systemSizeKw": self.system_size_kw,
            "dailyGenerationUnits": self.daily_generation_units,
            "annualOutputKwh": self.annual_output_kwh,
            "monthlySavingsLow": self.monthly_savings_low,
            "monthlySavingsHigh": self.monthly_savings_high,
            "monthlySavings": self.monthly_savings,
            "dailySavings": self.daily_savings,
            "annualSavingsAmount": self.annual_savings_amount,
            "lifetimeSavingsAmount": projection.lifetime_savings_amount,
            "returnPeriodMonths": self.return_period_months,
            "postSolarBillLow": self.post_solar_bill_low,
            "postSolarBillHigh": self.post_solar_bill_high,
            "currentMonthlyBill": self.current_monthly_bill,
            "currentBillBreakdown": self.current_bill_breakdown.as_dict() if self.current_bill_breakdown else None,
            "postSolarBillBreakdown": self.post_solar_bill_breakdown.as_dict() if self.post_solar_bill_breakdown else None,
            "tariffEscalationLow": projection.tariff_escalation_low,
            "tariffEscalationHigh": projection.tariff_escalation_high,
            "lifetimeKSEBSpend": projection.lifetime_kseb_spend,
            "lifetimeYears": projection.lifetime_years,
            "graphPoints": [point.as_dict() for point in projection.graph_points] if projection.graph_points is not None else None,
            "selfConsumptionModel": str(SelfConsumptionModel.FULL_OFFSET),
            "assumptions": {
                "selfConsumption": "FULL_OFFSET — generation offsets grid purchase, capped at consumption",
                "degradation": (
                    f"APPROVED_CONFIG — {canonical(projection.degradation_rate_per_year)}% per year applied to the offset" if projection.available else "NOT_APPROVED — no degradation applied"
                ),
                "tariffEscalation": (
                    f"APPROVED_CONFIG — {canonical(projection.tariff_escalation_low)}%–{canonical(projection.tariff_escalation_high)}% per year (graph uses the midpoint)"
                    if projection.available
                    else "NOT_APPROVED — no escalation applied"
                ),
                "exportCompensation": "NOT_APPROVED — surplus generation not valued",
                "savingsBasis": self.savings_basis,
                "paybackBasis": self.payback_basis,
            },
        }


def normalize_cycle(cycle: Any) -> str:
    """``normalizeCycle``: ``'bimonthly'`` when the text (spaces, hyphens, underscores removed) says so, else monthly."""
    if not cycle:
        return "monthly"
    raw = str(cycle).lower().replace(" ", "").replace("-", "").replace("_", "")
    for whitespace in "\t\n\r\f\v":
        raw = raw.replace(whitespace, "")
    return "bimonthly" if "bimonthly" in raw else "monthly"


def _positive(value: Any) -> bool:
    return is_number(value) and value > 0


@dataclass(frozen=True)
class SavingsInputs:
    """``calculateSavings``'s inputs: the energy result, the gross investment (payback basis) and, for the V1
    fallback only, the customer's current bill and its cycle.

    The two amounts may be Decimals, ints or numeric strings and are stored as Decimals (the JavaScript coerces a
    numeric string: ``"229000" > 0``); floats raise ``TypeError``, anything unparsable ``ValueError``."""

    energy_profile: EnergyProfile | EnergyBlocked | None
    customer_total_including_gst: Decimal | int | None = None
    current_bill_amount: Decimal | int | None = None
    current_bill_cycle: str | None = "monthly"

    def __post_init__(self) -> None:
        for name in ("customer_total_including_gst", "current_bill_amount"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, to_decimal(getattr(self, name), field=name))


@exact
def calculate_savings(inputs: SavingsInputs, *, region: RegionConfig | None = None, config: SavingsConfig | None = None) -> SavingsResult | SavingsBlocked:
    """``calculateSavings`` (spec §3). ``region`` (the energy result's region tariff) selects V2, the bill
    difference; without it V1 (generation × effective rate). ``config`` gates the lifetime projection."""
    energy_profile = inputs.energy_profile
    customer_total_including_gst = inputs.customer_total_including_gst
    current_bill_amount, current_bill_cycle = inputs.current_bill_amount, inputs.current_bill_cycle
    if energy_profile is None or not energy_profile.available:
        return SavingsBlocked(SavingsError.MISSING_ENERGY_RESULT, "No Energy Engine result available.")
    profile: EnergyProfile = energy_profile  # type: ignore[assignment]
    rate = profile.average_tariff_rate
    generation = profile.monthly_generation
    consumption = profile.monthly_consumption
    if rate is None or rate <= 0:
        return SavingsBlocked(SavingsError.MISSING_TARIFF, "No valid average tariff rate from Energy Engine.")
    if generation is None or generation <= 0:
        return SavingsBlocked(SavingsError.ZERO_GENERATION, "No generation estimate from Energy Engine.")
    if consumption is None or consumption <= 0:
        return SavingsBlocked(SavingsError.ZERO_CONSUMPTION, "No consumption estimate from Energy Engine.")

    phase = profile.phase or Phase.SINGLE
    effective_generation = min(generation, consumption)
    post_solar_consumption = max(consumption - generation, ZERO)
    generation_low = js_round(profile.daily_generation_low * 30)
    generation_high = js_round(profile.daily_generation_high * 30)
    post_solar_consumption_high = max(consumption - generation_low, ZERO)  # low generation → high residual
    post_solar_consumption_low = max(consumption - generation_high, ZERO)  # high generation → low residual

    current_breakdown = post_breakdown = None
    if region is not None:
        current = units_to_bill(consumption * 2, region, phase)
        current_monthly_bill = js_round(current.total / 2)
        post = units_to_bill(post_solar_consumption * 2, region, phase)
        monthly_savings = max(ZERO, current_monthly_bill - js_round(post.total / 2))
        post_solar_bill_low = js_round(units_to_bill(post_solar_consumption_low * 2, region, phase).total / 2)
        post_solar_bill_high = js_round(units_to_bill(post_solar_consumption_high * 2, region, phase).total / 2)
        monthly_savings_high = max(ZERO, current_monthly_bill - post_solar_bill_low)
        monthly_savings_low = max(ZERO, current_monthly_bill - post_solar_bill_high)
        current_breakdown, post_breakdown = MonthlyBillBreakdown.of(current), MonthlyBillBreakdown.of(post)
        basis = BILL_DIFFERENCE_BASIS
    else:
        monthly_savings = js_round(effective_generation * rate)
        monthly_savings_low = js_round(min(generation_low, consumption) * rate)
        monthly_savings_high = js_round(min(generation_high, consumption) * rate)
        if _positive(current_bill_amount):
            amount = Decimal(current_bill_amount)
            current_monthly_bill = js_round(amount / 2) if normalize_cycle(current_bill_cycle) == "bimonthly" else amount
        else:
            current_monthly_bill = js_round(consumption * rate)
        post_solar_bill_high = max(ZERO, current_monthly_bill - monthly_savings_low)
        post_solar_bill_low = max(ZERO, current_monthly_bill - monthly_savings_high)
        basis = FIRST_YEAR_BASIS

    annual_savings = monthly_savings * 12
    return_period = None
    if _positive(customer_total_including_gst) and annual_savings > 0:
        return_period = (Decimal(customer_total_including_gst) * 12 / annual_savings).to_integral_value(rounding=ROUND_CEILING)
    projection = project_lifetime(config or SavingsConfig(), current_monthly_bill, post_solar_bill_low, post_solar_bill_high, monthly_savings, customer_total_including_gst)
    system_size = profile.recommended_system_size_kw
    daily_units = None
    if system_size is not None and profile.daily_generation_low is not None and profile.daily_generation_high is not None:
        daily_units = round_places((profile.daily_generation_low + profile.daily_generation_high) / 2, 1)
    return SavingsResult(
        system_size_kw=system_size,
        daily_generation_units=daily_units,
        annual_output_kwh=profile.annual_generation,
        monthly_savings_low=monthly_savings_low,
        monthly_savings_high=monthly_savings_high,
        monthly_savings=monthly_savings,
        daily_savings=js_round(monthly_savings / 30),
        annual_savings_amount=annual_savings,
        return_period_months=return_period,
        post_solar_bill_low=post_solar_bill_low,
        post_solar_bill_high=post_solar_bill_high,
        current_monthly_bill=current_monthly_bill,
        current_bill_breakdown=current_breakdown,
        post_solar_bill_breakdown=post_breakdown,
        projection=projection,
        savings_basis=basis,
        payback_basis=("GROSS — uses customerTotalIncludingGST, subsidy not deducted" if customer_total_including_gst is not None else "UNAVAILABLE — no investment amount provided"),
    )
