"""Finance engine: reducing-balance EMI (PMT) with indicative interest-rate tiers.

Port of Flarize ``src/lib/financeEngine.js`` (version ``'1.1.0'``, spec §5), a protected formula (spec §19):
``EMI = round(P × r × (1+r)^n / ((1+r)^n − 1))`` with ``r`` the monthly rate and ``n`` months (``round(P / n)`` at
0 %); ``totalRepayment = EMI × n``; ``totalInterest = totalRepayment − P``; ``dailyPayment = round(EMI / 30)``.

D-7: the interest tiers (Flarize 5.75 % / 7.9 %; the website EMI rules say 5.75 % / 8 %) are **data** — a
:class:`FinanceConfig` built from ``pricing_cost_config`` ``finance.*`` rows or the legacy ``finance-config.json``;
no rate is written in this module. The only literal is the JavaScript's fallback for a configuration with neither
tiers nor ``defaults.annualInterestRate`` (:data:`LEGACY_FALLBACK_ANNUAL_INTEREST_RATE`), kept for parity.

The down payment and the financed principal (``quotationWorkspace.resolveFinanceResult``: down payment on the
**original gross, before subsidy**; principal = gross − down payment − subsidy) are :func:`finance_basis` and
:func:`resolve_finance`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from engines.money import HUNDRED, ONE, ZERO, exact, is_number, js_round, js_text, json_number

FINANCE_ENGINE_VERSION = "1.1.0"
FINANCE_SOURCE = "FINANCE_ENGINE"
FINANCE_STATUS_LIVE = "LIVE"

#: ``financeConfig.defaults.annualInterestRate ?? 7`` — used only when a configuration has no tiers and no default.
LEGACY_FALLBACK_ANNUAL_INTEREST_RATE = Decimal(7)
#: JavaScript fallbacks for missing configuration keys.
DEFAULT_TENURE_YEARS = Decimal(10)
DEFAULT_MAX_ANNUAL_INTEREST_RATE = Decimal(18)
DEFAULT_MIN_TENURE_YEARS = Decimal(1)
DEFAULT_MAX_TENURE_YEARS = Decimal(10)


class FinanceError(StrEnum):
    INVALID_PRINCIPAL = "INVALID_PRINCIPAL"
    INVALID_INTEREST_RATE = "INVALID_INTEREST_RATE"
    INVALID_TENURE = "INVALID_TENURE"
    MISSING_CONFIG = "MISSING_CONFIG"


@dataclass(frozen=True)
class RateTier:
    """An indicative rate for principals in [``min_finance_amount``, ``max_finance_amount``] (``None`` = open)."""

    annual_interest_rate: Decimal | None
    min_finance_amount: Decimal | None = None
    max_finance_amount: Decimal | None = None
    label: str | None = None


@dataclass(frozen=True)
class FinanceConfig:
    """``finance-config.json`` / ``pricing_cost_config`` ``finance.*``: tiers, defaults, validation bounds."""

    config_version: str | None = None
    rate_tiers: tuple[RateTier, ...] = ()
    default_tenure_years: Decimal | int | None = None
    default_annual_interest_rate: Decimal | None = None
    max_annual_interest_rate: Decimal | None = None
    min_tenure_years: Decimal | None = None
    max_tenure_years: Decimal | None = None
    down_payment_percentage: Decimal | None = None
    down_payment_note: str | None = None
    rate_qualification: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FinanceConfig:
        defaults = data.get("defaults") if isinstance(data.get("defaults"), Mapping) else {}
        validation = data.get("validation") if isinstance(data.get("validation"), Mapping) else {}
        down_payment = data.get("downPayment") if isinstance(data.get("downPayment"), Mapping) else {}
        source = data.get("source") if isinstance(data.get("source"), Mapping) else {}
        tiers = data.get("rateTiers") if isinstance(data.get("rateTiers"), list) else []
        return cls(
            config_version=data.get("configVersion"),
            rate_tiers=tuple(
                RateTier(json_number(tier.get("annualInterestRate")), json_number(tier.get("minFinanceAmount")), json_number(tier.get("maxFinanceAmount")), tier.get("label"))
                for tier in tiers
                if isinstance(tier, Mapping)
            ),
            default_tenure_years=json_number(defaults.get("tenureYears")),
            default_annual_interest_rate=json_number(defaults.get("annualInterestRate")),
            max_annual_interest_rate=json_number(validation.get("maxAnnualInterestRate")),
            min_tenure_years=json_number(validation.get("minTenureYears")),
            max_tenure_years=json_number(validation.get("maxTenureYears")),
            down_payment_percentage=json_number(down_payment.get("percentage")),
            down_payment_note=down_payment.get("note"),
            rate_qualification=source.get("rateQualification"),
        )


def validate_finance_config(data: Any) -> tuple[bool, list[str]]:
    """``validateFinanceConfig`` on the raw JSON: ``(valid, errors)``."""
    if not isinstance(data, Mapping):
        return False, ["Config must be a non-null object"]
    errors = []

    def positive(section: Mapping[str, Any], key: str) -> bool:
        value = section.get(key)
        return value is not None and not (isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and value <= 0)

    defaults = data.get("defaults")
    if not isinstance(defaults, Mapping):
        errors.append("Missing defaults section")
    elif not positive(defaults, "tenureYears"):
        errors.append("defaults.tenureYears must be a positive number")
    validation = data.get("validation")
    if not isinstance(validation, Mapping):
        errors.append("Missing validation section")
    else:
        for key in ("maxAnnualInterestRate", "minTenureYears", "maxTenureYears"):
            if not positive(validation, key):
                errors.append(f"validation.{key} must be a positive number")
    tiers = data.get("rateTiers")
    if not isinstance(tiers, list) or not tiers:
        errors.append("rateTiers must be a non-empty array")
    else:
        for index, tier in enumerate(tiers):
            rate = tier.get("annualInterestRate") if isinstance(tier, Mapping) else None
            if rate is None or (isinstance(rate, (int, float, Decimal)) and not isinstance(rate, bool) and rate < 0):
                errors.append(f"rateTiers[{index}].annualInterestRate must be a non-negative number")
    return not errors, errors


@dataclass(frozen=True)
class IndicativeRate:
    rate: Decimal | None
    label: str | None


def resolve_indicative_rate(principal: Decimal | int, config: FinanceConfig | None) -> IndicativeRate:
    """``resolveIndicativeRate``: the first tier whose bounds hold the principal; none → the last tier; no tiers →
    ``defaults.annualInterestRate`` (else the legacy 7 %)."""
    tiers = config.rate_tiers if config is not None else ()
    if not tiers:
        fallback = config.default_annual_interest_rate if config is not None else None
        return IndicativeRate(fallback if fallback is not None else LEGACY_FALLBACK_ANNUAL_INTEREST_RATE, None)
    for tier in tiers:
        min_ok = tier.min_finance_amount is None or principal >= tier.min_finance_amount
        max_ok = tier.max_finance_amount is None or principal <= tier.max_finance_amount
        if min_ok and max_ok:
            return IndicativeRate(tier.annual_interest_rate, tier.label)
    return IndicativeRate(tiers[-1].annual_interest_rate, tiers[-1].label)


# ---- results -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FinanceBlocked:
    status: FinanceError
    reason: str
    calculated_at: str | None
    available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": FINANCE_SOURCE,
            "status": str(self.status),
            "available": False,
            "reason": self.reason,
            "engineVersion": FINANCE_ENGINE_VERSION,
            "loanAmount": ZERO,
            "interestRatePct": ZERO,
            "rateLabel": None,
            "tenureYears": ZERO,
            "tenureMonths": ZERO,
            "monthlyEMI": ZERO,
            "dailyPayment": None,
            "totalInterest": ZERO,
            "totalRepayment": ZERO,
            "downPaymentPercentage": None,
            "downPaymentAmount": None,
            "grossQuotationAmount": None,
            "postSubsidyInvestment": None,
            "calculatedAt": self.calculated_at,
        }


@dataclass(frozen=True)
class FinanceResult:
    loan_amount: Decimal
    interest_rate_pct: Decimal
    rate_label: str | None
    tenure_years: Decimal
    tenure_months: Decimal
    monthly_emi: Decimal
    daily_payment: Decimal
    total_interest: Decimal
    total_repayment: Decimal
    down_payment_percentage: Decimal | None
    down_payment_amount: Decimal | None
    gross_quotation_amount: Decimal | None
    post_subsidy_investment: Decimal | None
    calculated_at: str | None
    config_version: str | None
    rate_qualification: str | None
    down_payment_note: str | None
    available: bool = True
    status: str = FINANCE_STATUS_LIVE

    def as_dict(self) -> dict[str, Any]:
        """The JavaScript result object (the quotation payload contract), key for key."""
        return {
            "source": FINANCE_SOURCE,
            "status": FINANCE_STATUS_LIVE,
            "available": True,
            "engineVersion": FINANCE_ENGINE_VERSION,
            "loanAmount": self.loan_amount,
            "interestRatePct": self.interest_rate_pct,
            "rateLabel": self.rate_label,
            "tenureYears": self.tenure_years,
            "tenureMonths": self.tenure_months,
            "monthlyEMI": self.monthly_emi,
            "dailyPayment": self.daily_payment,
            "totalInterest": self.total_interest,
            "totalRepayment": self.total_repayment,
            "downPaymentPercentage": self.down_payment_percentage,
            "downPaymentAmount": self.down_payment_amount,
            "grossQuotationAmount": self.gross_quotation_amount,
            "postSubsidyInvestment": self.post_subsidy_investment,
            "calculatedAt": self.calculated_at,
            "configVersion": self.config_version,
            "assumptions": {
                "formula": "STANDARD_PMT_REDUCING_BALANCE",
                "rounding": "MATH_ROUND_MONTHLY_EMI",
                "totalRepaymentBasis": "ROUNDED_EMI_TIMES_MONTHS",
                "totalInterestBasis": "TOTAL_REPAYMENT_MINUS_PRINCIPAL",
                "rateSource": "INDICATIVE_CONFIG_TIER",
                "rateQualification": self.rate_qualification,
                "downPaymentNote": self.down_payment_note,
            },
        }


def _or(value: Any, fallback: Decimal) -> Any:
    return fallback if value is None else value


def _refuse_floats(**values: Any) -> None:
    for name, value in values.items():
        if isinstance(value, float):
            raise TypeError(f"{name}: float is not accepted in an engine; pass a Decimal or an int")


@dataclass(frozen=True)
class FinanceInputs:
    """``calculateFinance``'s inputs: the principal, an explicit rate (else the principal's tier), a tenure (else the
    configured default), and the down-payment figures passed through into the payload. Floats raise ``TypeError``."""

    principal: Any
    annual_interest_rate: Any = None
    tenure_years: Any = None
    down_payment_percentage: Decimal | int | None = None
    down_payment_amount: Decimal | int | None = None
    gross_quotation_amount: Decimal | int | None = None
    post_subsidy_investment: Decimal | int | None = None

    def __post_init__(self) -> None:
        _refuse_floats(**{name: getattr(self, name) for name in self.__dataclass_fields__})


@exact
def calculate_finance(inputs: FinanceInputs, config: FinanceConfig | None, *, calculated_at: str | None = None) -> FinanceResult | FinanceBlocked:
    """``calculateFinance`` (spec §5); ``calculated_at`` is the caller's ISO timestamp (engines never read the clock)."""
    principal, annual_interest_rate, tenure_years = inputs.principal, inputs.annual_interest_rate, inputs.tenure_years
    if config is None:
        return FinanceBlocked(FinanceError.MISSING_CONFIG, "Finance configuration is required.", calculated_at)
    tenure = tenure_years if tenure_years is not None else _or(config.default_tenure_years, DEFAULT_TENURE_YEARS)
    if not is_number(principal) or principal < 0:
        return FinanceBlocked(FinanceError.INVALID_PRINCIPAL, f"Principal must be a non-negative finite number. Received: {_received(principal)}", calculated_at)
    principal = Decimal(principal)
    rate_label = None
    if annual_interest_rate is not None:
        rate = annual_interest_rate
    else:
        resolved = resolve_indicative_rate(principal, config)
        rate, rate_label = resolved.rate, resolved.label
    if not is_number(rate) or rate < 0:
        return FinanceBlocked(FinanceError.INVALID_INTEREST_RATE, f"Annual interest rate must be a non-negative finite number. Received: {_received(rate)}", calculated_at)
    rate = Decimal(rate)
    max_rate = _or(config.max_annual_interest_rate, DEFAULT_MAX_ANNUAL_INTEREST_RATE)
    if rate > max_rate:
        return FinanceBlocked(FinanceError.INVALID_INTEREST_RATE, f"Annual interest rate {js_text(rate)}% exceeds maximum {js_text(max_rate)}%.", calculated_at)
    min_tenure = _or(config.min_tenure_years, DEFAULT_MIN_TENURE_YEARS)
    max_tenure = _or(config.max_tenure_years, DEFAULT_MAX_TENURE_YEARS)
    if not is_number(tenure) or tenure < min_tenure or tenure > max_tenure:
        return FinanceBlocked(FinanceError.INVALID_TENURE, f"Tenure must be between {js_text(min_tenure)} and {js_text(max_tenure)} years. Received: {_received(tenure)}", calculated_at)
    tenure = Decimal(tenure)
    months = tenure * 12
    if principal == 0:
        emi = ZERO
        total_repayment = ZERO
        total_interest = ZERO
    else:
        if rate == 0:
            emi = js_round(principal / months)
        else:
            monthly_rate = rate / 12 / HUNDRED
            factor = (ONE + monthly_rate) ** months
            emi = js_round(principal * monthly_rate * factor / (factor - 1))
        total_repayment = emi * months
        total_interest = total_repayment - principal
    return FinanceResult(
        loan_amount=principal,
        interest_rate_pct=rate,
        rate_label=rate_label,
        tenure_years=tenure,
        tenure_months=months,
        monthly_emi=emi,
        daily_payment=js_round(emi / 30),
        total_interest=total_interest,
        total_repayment=total_repayment,
        down_payment_percentage=inputs.down_payment_percentage,
        down_payment_amount=inputs.down_payment_amount,
        gross_quotation_amount=inputs.gross_quotation_amount,
        post_subsidy_investment=inputs.post_subsidy_investment,
        calculated_at=calculated_at,
        config_version=config.config_version,
        rate_qualification=config.rate_qualification,
        down_payment_note=config.down_payment_note,
    )


def _received(value: Any) -> str:
    """``${value}`` in the JavaScript's "Received: …" messages (``undefined`` for a missing rate)."""
    return js_text(value)


# ---- down payment and principal (quotationWorkspace.resolveFinanceResult) ------------------------------------------


@dataclass(frozen=True)
class FinanceBasis:
    """The financed amount of a quotation: down payment on the ORIGINAL gross (before subsidy), then subsidy."""

    gross_quotation_amount: Decimal
    down_payment_percentage: Decimal
    down_payment_amount: Decimal
    subsidy_amount: Decimal
    post_subsidy_investment: Decimal
    principal: Decimal


@exact
def finance_basis(gross_quotation_amount: Decimal | int, down_payment_percentage: Decimal | int | None, subsidy_amount: Decimal | int | None) -> FinanceBasis:
    """``dp = round(gross × dp% / 100)``; ``postSubsidyInvestment = max(0, gross − subsidy)``;
    ``principal = max(0, (gross − dp) − subsidy)``. A missing percentage is 0; a missing subsidy is 0."""
    _refuse_floats(gross_quotation_amount=gross_quotation_amount, down_payment_percentage=down_payment_percentage, subsidy_amount=subsidy_amount)
    gross = Decimal(gross_quotation_amount)
    percentage = Decimal(down_payment_percentage) if down_payment_percentage is not None else ZERO
    subsidy = Decimal(subsidy_amount) if subsidy_amount is not None else ZERO
    down_payment = js_round(gross * percentage / HUNDRED)
    return FinanceBasis(
        gross_quotation_amount=gross,
        down_payment_percentage=percentage,
        down_payment_amount=down_payment,
        subsidy_amount=subsidy,
        post_subsidy_investment=max(ZERO, gross - subsidy),
        principal=max(ZERO, (gross - down_payment) - subsidy),
    )


def _available_subsidy(subsidy: Any) -> Decimal:
    """``subsidyResult.available ? (subsidyResult.totalSubsidy ?? 0) : 0`` for a subsidy result object or its payload
    (``as_dict()``, the JavaScript result object)."""
    if subsidy is None:
        return ZERO
    if isinstance(subsidy, Mapping):  # a payload: JSON numbers, parsed with or without parse_float=Decimal
        total = json_number(subsidy.get("totalSubsidy")) if subsidy.get("available") else None
    else:
        total = getattr(subsidy, "total_subsidy", None) if getattr(subsidy, "available", False) else None
        _refuse_floats(total_subsidy=total)
    return ZERO if total is None else Decimal(total)


def resolve_finance(gross_quotation_amount: Decimal | int | None, subsidy: Any, config: FinanceConfig | None, *, calculated_at: str | None = None) -> FinanceResult | FinanceBlocked | None:
    """``resolveFinanceResult``: ``None`` without a configuration or a positive gross; otherwise the EMI on
    :func:`finance_basis`'s principal. ``subsidy`` is a subsidy result or its payload (``as_dict()``) — only an
    *available* one counts (GIVE_IT_UP and ineligible results count 0) — or ``None``. A float gross raises
    ``TypeError``."""
    _refuse_floats(gross_quotation_amount=gross_quotation_amount)
    if config is None:
        return None
    if not is_number(gross_quotation_amount) or gross_quotation_amount <= 0:
        return None
    subsidy_amount = _available_subsidy(subsidy)
    basis = finance_basis(gross_quotation_amount, config.down_payment_percentage, subsidy_amount)
    inputs = FinanceInputs(
        principal=basis.principal,
        down_payment_percentage=basis.down_payment_percentage,
        down_payment_amount=basis.down_payment_amount,
        gross_quotation_amount=basis.gross_quotation_amount,
        post_subsidy_investment=basis.post_subsidy_investment,
    )
    return calculate_finance(inputs, config, calculated_at=calculated_at)
