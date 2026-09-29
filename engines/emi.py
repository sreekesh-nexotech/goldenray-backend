"""The website EMI calculator of the legacy main backend, as pure functions (PLAN §3.3 ``calculators/emi*``).

==========================  ==============================================================  =============================
Function                    Legacy (``goldenray/views/emicalculator/views.py``, ``utils/emi``)  Platform endpoint
==========================  ==============================================================  =============================
:func:`calculate`           ``POST /api/emi-calculator/`` → ``emi.calculate`` + legacy keys  ``calculators/emi/``
:func:`quotation`           ``POST /api/emi-calculator/quotation/`` → ``quotation_breakdown``  ``calculators/emi/quotation/``
==========================  ==============================================================  =============================

(``GET /api/emi-calculator/config/`` is a serialisation of the configuration; the ``emi`` app builds it.)

The flow, as the legacy module documents it::

    system_cost   = price_per_kW × capacity_kW, or the customer's slider price (clamped to the size's band)
    down_payment  = system_cost × down_payment_percent / 100 (slider, clamped to the settings' band)
    subsidy       = the highest-priority active subsidy rule for the capacity — only "with subsidy"
    rate_basis    = system_cost − down_payment            (picks the rate band)
    loan_amount   = system_cost − down_payment − subsidy  (what the EMI is on)
    rate          = the best interest rule for capacity / system cost / rate basis (priority, then specificity)
    emi           = reducing-balance EMI on loan_amount (binary64, rounded to paise — ``utils/finance.emi_calc``)
    daily         = emi ÷ daily_saving_divisor

Faithful on purpose (docs/decisions/calculators-emi.md): Decimal money in Python's default context (28 digits,
``ROUND_HALF_UP`` paise), the EMI itself in binary64 (``engines.finance`` rounds the EMI to whole rupees in a 60-digit
context — a different published number, so it is not used), rule order as the legacy ORM ordering, value parsing as
the legacy view (``float()``/``int()`` of any JSON value, ``or`` fallbacks). Differences: sizes and rules are
identified by ``uid`` (``size_uid``, ``rule_uid``) instead of integer ids; a subsidy rule may add ``amount_per_kw``
and a ``cap_amount`` (PLAN §2.8 columns; the legacy rows import with 0 and no cap, which is the legacy amount);
whatever made the legacy view crash (500) is ``EmiError("invalid_input", …)`` (400).
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from engines.legacy_lookups import LegacyCrash, check_renderable, crash_detail, decimal_param
from engines.website_calculators import emi_with_interest

INVALID_INPUT_MESSAGE = "The EMI calculator cannot process these inputs."
SIZE_CAPACITY_MAX_DIGITS = 6  # legacy ``emi_system_size.capacity_kw`` numeric(6,2): float lookups rounded to 6 digits
MAX_QUOTATION_PACKAGES = 6
QUOTATION_DEFAULT_TENURE_YEARS = 10
PAISE = Decimal("0.01")
HUNDRED = Decimal("100")
ZERO = Decimal("0")


class EmiError(Exception):
    """A 400 the legacy endpoint answered with ``{"error": message}``."""

    def __init__(self, code: str, message: str, status: int = 400, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


# ── configuration ─────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EmiSettings:
    """The calculator's knobs (legacy ``emi_calculator_settings``); percentages in percent, as the legacy columns."""

    tenure_min_years: int = 1
    tenure_max_years: int = 10
    tenure_default_years: int = 5
    daily_saving_divisor: int = 30
    price_step: Decimal = Decimal("5000.00")
    down_payment_min_percent: Decimal = Decimal("10.00")
    down_payment_max_percent: Decimal = Decimal("90.00")
    down_payment_step_percent: Decimal = Decimal("5.00")
    down_payment_quick_adds: tuple[Decimal, ...] = ()
    rate_max: Decimal = Decimal("18.00")
    default_interest_rate: Decimal = Decimal("9.50")
    panel_life_years: int = 25


@dataclass(frozen=True)
class SystemSize:
    """A priced system size (legacy ``emi_system_size``, or a pack of the current release)."""

    uid: str
    label: str
    capacity_kw: Decimal
    price_per_kw: Decimal
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    monthly_bill_reference: Decimal = ZERO
    sort_order: int = 0
    position: int = 0
    system_cost: Decimal | None = None  # a lump-sum price; the legacy price is price_per_kw × capacity


@dataclass(frozen=True)
class SubsidyRule:
    uid: str
    label: str
    min_kw: Decimal | None
    max_kw: Decimal | None
    amount: Decimal
    priority: int = 0
    amount_per_kw: Decimal = ZERO
    cap_amount: Decimal | None = None
    position: int = 0

    def matches(self, capacity_kw) -> bool:
        if self.min_kw is not None and capacity_kw < self.min_kw:
            return False
        if self.max_kw is not None and capacity_kw > self.max_kw:
            return False
        return True

    def subsidy_for(self, capacity_kw: Decimal) -> Decimal:
        """The legacy flat ``amount``; plus ``amount_per_kw`` × kW (rounded to paise, half-up, like every amount of
        the calculator) and capped at ``cap_amount`` when those are set."""
        amount = Decimal(self.amount)
        if self.amount_per_kw:
            amount = _money(amount + Decimal(self.amount_per_kw) * capacity_kw)
        if self.cap_amount is not None:
            amount = min(amount, Decimal(self.cap_amount))
        return amount


@dataclass(frozen=True)
class InterestRule:
    uid: str
    label: str
    rate: Decimal  # percent
    min_rate: Decimal  # percent
    is_locked: bool = False
    priority: int = 0
    min_kw: Decimal | None = None
    max_kw: Decimal | None = None
    min_cost: Decimal | None = None
    max_cost: Decimal | None = None
    min_loan: Decimal | None = None
    max_loan: Decimal | None = None
    position: int = 0

    def matches(self, capacity_kw, loan_amount, system_cost=None) -> bool:
        """True when every configured band admits these values (an unset band never rejects)."""
        bands = ((capacity_kw, self.min_kw, self.max_kw), (system_cost, self.min_cost, self.max_cost), (loan_amount, self.min_loan, self.max_loan))
        for value, lower, upper in bands:
            if value is None:
                if lower is not None or upper is not None:
                    return False
                continue
            if lower is not None and value < lower:
                return False
            if upper is not None and value > upper:
                return False
        return True

    @property
    def specificity(self) -> int:
        return sum(1 for bound in (self.min_kw, self.max_kw, self.min_cost, self.max_cost, self.min_loan, self.max_loan) if bound is not None)


@dataclass(frozen=True)
class EmiConfig:
    """Everything the calculator reads: settings and the *active* sizes and rules (any order; sorted here)."""

    settings: EmiSettings
    sizes: tuple[SystemSize, ...] = ()
    subsidy_rules: tuple[SubsidyRule, ...] = ()
    interest_rules: tuple[InterestRule, ...] = ()

    def ordered_sizes(self) -> list[SystemSize]:
        """Legacy ``EmiSystemSize`` ordering: ``sort_order``, ``capacity_kw``."""
        return sorted(self.sizes, key=lambda size: (size.sort_order, size.capacity_kw, size.position))

    def ordered_subsidy_rules(self) -> list[SubsidyRule]:
        """Legacy ordering ``-priority, min_kw`` (PostgreSQL: NULLs last)."""
        return sorted(self.subsidy_rules, key=lambda rule: (-rule.priority, _nulls_last(rule.min_kw), rule.position))

    def ordered_interest_rules(self) -> list[InterestRule]:
        """Legacy ordering ``-priority, min_kw, min_cost, min_loan`` (NULLs last)."""
        return sorted(self.interest_rules, key=lambda rule: (-rule.priority, _nulls_last(rule.min_kw), _nulls_last(rule.min_cost), _nulls_last(rule.min_loan), rule.position))


def _nulls_last(value: Decimal | None) -> tuple:
    return (1, ZERO) if value is None else (0, value)


# ── helpers of the legacy module ──────────────────────────────────────────────────────────────────────────────────
def _money(value) -> Decimal:
    """Round to paise, half-up — the convention of the legacy app."""
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def _dec(value, default=None):
    if value is None or value == "":
        return default
    return Decimal(str(value))


def _to_float(value):
    if value is None or value == "":
        return None
    return float(value)


def _to_int(value):
    if value is None or value == "":
        return None
    return int(value)


def _to_uid(value) -> str | None:
    """``size_uid``: absent/empty/falsy → ``None``; anything but a UUID text is an invalid value."""
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("size_uid must be a uid")
    return str(uuid.UUID(value))


def _to_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def check_renderable_error(message: str) -> None:
    """An error answer carrying text that is not UTF-8 (a lone surrogate in a package key) crashed the legacy
    renderer: the answer is ``invalid_input`` instead."""
    try:
        check_renderable(message)
    except LegacyCrash as exc:
        raise EmiError("invalid_input", INVALID_INPUT_MESSAGE, detail=crash_detail(exc)) from None


def _run(compute: Callable[[], dict]) -> dict:
    try:
        with decimal.localcontext(decimal.Context()):
            result = compute()
            check_renderable(result)
            return result
    except EmiError as exc:
        check_renderable_error(exc.message)  # a message quoting a package key the legacy renderer could not encode
        raise
    except (LegacyCrash, ArithmeticError, AttributeError, LookupError, TypeError, ValueError) as exc:
        raise EmiError("invalid_input", INVALID_INPUT_MESSAGE, detail=crash_detail(exc)) from exc


# ── policy lookups ────────────────────────────────────────────────────────────────────────────────────────────────
def resolve_subsidy(config: EmiConfig, capacity_kw) -> Decimal:
    """Subsidy for a capacity (the first matching rule in legacy order), or 0 when no band matches."""
    if capacity_kw is None:
        return Decimal("0")
    rules = [rule for rule in config.ordered_subsidy_rules() if rule.matches(capacity_kw)]
    if not rules:
        return Decimal("0")
    return rules[0].subsidy_for(capacity_kw)


def resolve_interest_rule(config: EmiConfig, capacity_kw, loan_amount, system_cost=None) -> InterestRule | None:
    """Priority first; the more specific rule wins ties; then the legacy query order (a stable sort)."""
    candidates = [rule for rule in config.ordered_interest_rules() if rule.matches(capacity_kw, loan_amount, system_cost)]
    if not candidates:
        return None
    candidates.sort(key=lambda rule: (rule.priority, rule.specificity), reverse=True)
    return candidates[0]


def resolve_rate(config: EmiConfig, capacity_kw, rate_basis, system_cost, requested_rate=None):
    """``(rate, base_rate, floor_rate, locked, rule)`` — shared by the calculator and the quotation document."""
    rule = resolve_interest_rule(config, capacity_kw, rate_basis, system_cost)
    if rule is None:
        base_rate = Decimal(config.settings.default_interest_rate)
        floor_rate = base_rate
        locked = False
    else:
        base_rate = Decimal(rule.rate)
        floor_rate = Decimal(rule.min_rate)
        locked = rule.is_locked
    if locked:
        rate = base_rate
    elif requested_rate is not None:
        rate = max(requested_rate, floor_rate)
    else:
        rate = max(base_rate, floor_rate)
    return rate, base_rate, floor_rate, locked, rule


def resolve_rate_unlock(config: EmiConfig, capacity_kw, system_cost, loan_amount, current_rate, down_payment_amount, max_down_payment):
    """The cheapest rate band reachable by paying more upfront (extra rounded up to ₹100), or ``None``."""
    best = None
    for rule in config.ordered_interest_rules():
        if rule.max_loan is None or Decimal(rule.rate) >= current_rate:
            continue
        if Decimal(rule.max_loan) >= loan_amount:
            continue
        if not rule.matches(capacity_kw, Decimal(rule.max_loan), system_cost):
            continue
        extra = loan_amount - Decimal(rule.max_loan)
        extra = (extra / 100).to_integral_value(rounding=ROUND_CEILING) * 100
        if down_payment_amount + extra > max_down_payment:
            continue
        if best is None or extra < best[0]:
            best = (extra, rule)
    if best is None:
        return None
    extra, rule = best
    return {"rate": float(rule.rate), "extra_down_payment": float(extra), "down_payment_amount": float(down_payment_amount + extra), "rule_label": rule.label}


def find_system_size(config: EmiConfig, capacity_kw=None, size_uid=None) -> SystemSize | None:
    """The active size by uid (``size_id`` in the legacy), else by capacity (``capacity_kw`` exact, 6 digits)."""
    sizes = config.ordered_sizes()
    if size_uid is not None:
        return next((size for size in sizes if size.uid == size_uid), None)
    if capacity_kw is not None:
        capacity = decimal_param(capacity_kw, SIZE_CAPACITY_MAX_DIGITS)
        return next((size for size in sizes if size.capacity_kw == capacity), None)
    return None


# ── POST calculators/emi/ ─────────────────────────────────────────────────────────────────────────────────────────
def calculate(body, config: EmiConfig) -> dict:
    """``EMICalculatorAPIView.post``: the full breakdown plus the flattened legacy headline keys."""
    return _run(lambda: _calculate_view(body, config))


def _calculate_view(request_data, config: EmiConfig) -> dict:
    data = request_data or {}
    try:
        capacity_kw = _to_float(data.get("capacity_kw") or data.get("power_capacity"))
        size_uid = _to_uid(data.get("size_uid"))
        tenure_years = _to_int(data.get("tenure_years"))
        interest_override = _to_float(data.get("interest_rate"))
        price_override = _to_float(data.get("system_cost") or data.get("price"))
        down_payment_override = _to_float(data.get("down_payment_percent"))
    except (TypeError, ValueError):
        raise EmiError("invalid_number", "Invalid numeric value in request") from None
    apply_down_payment = _to_bool(data.get("apply_down_payment"), default=True)
    apply_subsidy = _to_bool(data.get("apply_subsidy"), default=True)
    if size_uid is None and capacity_kw is None:
        raise EmiError("size_required", "Provide either size_uid or capacity_kw")
    try:
        breakdown = breakdown_for(
            config,
            capacity_kw=capacity_kw,
            size_uid=size_uid,
            tenure_years=tenure_years,
            interest_rate_override=interest_override,
            system_cost_override=price_override,
            down_payment_percent_override=down_payment_override,
            apply_down_payment=apply_down_payment,
            apply_subsidy=apply_subsidy,
        )
    except ValueError as exc:
        raise EmiError("invalid_request", str(exc)) from None
    return _with_legacy_keys(breakdown)


def breakdown_for(
    config: EmiConfig,
    capacity_kw=None,
    size_uid=None,
    tenure_years=None,
    interest_rate_override=None,
    system_cost_override=None,
    down_payment_percent_override=None,
    apply_down_payment=True,
    apply_subsidy=True,
) -> dict:
    """``utils/emi.calculate``: raises ``ValueError`` with a customer-safe message on bad input."""
    settings = config.settings
    size = find_system_size(config, capacity_kw=capacity_kw, size_uid=size_uid)
    if size is None and capacity_kw is None:
        raise ValueError("Provide either size_uid or capacity_kw")
    resolved_capacity = Decimal(size.capacity_kw) if size else _dec(capacity_kw)

    # Only a missing tenure falls back to the default; an explicit 0 is an error.
    tenure = settings.tenure_default_years if tenure_years is None else tenure_years
    try:
        tenure = int(tenure)
    except (TypeError, ValueError):
        raise ValueError("tenure_years must be a whole number of years") from None
    if tenure < settings.tenure_min_years or tenure > settings.tenure_max_years:
        raise ValueError(f"tenure_years must be between {settings.tenure_min_years} and {settings.tenure_max_years}")

    if size is None:
        raise ValueError(f"No active system size configured for {resolved_capacity} kW")
    price_per_kw = Decimal(size.price_per_kw)
    default_cost = _money(size.system_cost) if size.system_cost is not None else _money(price_per_kw * resolved_capacity)
    price_min = _dec(size.price_min, default=default_cost)
    price_max = _dec(size.price_max, default=default_cost)
    if system_cost_override is not None:
        system_cost = _money(_dec(system_cost_override))
        if system_cost <= 0:
            raise ValueError("System price must be greater than zero")
        system_cost = min(max(system_cost, price_min), price_max)
        price_source = "customer"
    else:
        system_cost = default_cost
        price_source = "computed"

    dp_min = Decimal(settings.down_payment_min_percent)
    dp_max = Decimal(settings.down_payment_max_percent)
    if apply_down_payment:
        requested_dp_percent = _dec(down_payment_percent_override)
        down_payment_percent = min(max(requested_dp_percent, dp_min), dp_max) if requested_dp_percent is not None else dp_min
        down_payment_amount = _money(system_cost * down_payment_percent / HUNDRED)
    else:
        down_payment_percent = Decimal("0")
        down_payment_amount = Decimal("0")

    subsidy = resolve_subsidy(config, resolved_capacity) if apply_subsidy else Decimal("0")
    subsidy = min(subsidy, system_cost)
    # rate_basis picks the interest band; the subsidy lowers the EMI but never moves the customer into a cheaper band.
    rate_basis = _money(max(Decimal("0"), system_cost - down_payment_amount))
    loan_amount = _money(max(Decimal("0"), rate_basis - subsidy))

    requested_rate = _dec(interest_rate_override)
    interest_rate, base_rate, floor_rate, locked, rule = resolve_rate(config, resolved_capacity, rate_basis, system_cost, requested_rate)
    unlock = resolve_rate_unlock(
        config,
        capacity_kw=resolved_capacity,
        system_cost=system_cost,
        loan_amount=rate_basis,
        current_rate=interest_rate,
        down_payment_amount=down_payment_amount,
        max_down_payment=_money(system_cost * dp_max / HUNDRED),
    )
    if unlock is not None:
        unlock["down_payment_percent"] = float(Decimal(str(unlock["down_payment_amount"])) / system_cost * 100)

    emi = emi_with_interest(principal=float(loan_amount), interest_rate=float(interest_rate), tenure_years=tenure)
    divisor = settings.daily_saving_divisor or 30
    daily_amount = round(emi["emi_per_month"] / divisor, 2)
    monthly_bill = Decimal(size.monthly_bill_reference) if size else Decimal("0")
    monthly_savings = float(monthly_bill) - emi["emi_per_month"]
    return {
        "system": {
            "size_uid": size.uid if size else None,
            "label": size.label if size else None,
            "capacity_kw": float(resolved_capacity),
            "price_per_kw": float(price_per_kw),
            "system_cost": float(system_cost),
            "price_min": float(price_min),
            "price_max": float(price_max),
            "price_source": price_source,
            "monthly_bill_reference": float(monthly_bill),
        },
        "down_payment": {
            "applied": bool(apply_down_payment),
            "percent": float(down_payment_percent),
            "amount": float(down_payment_amount),
            "min_percent": float(dp_min),
            "max_percent": float(dp_max),
            "step_percent": float(settings.down_payment_step_percent),
            "min_amount": float(_money(system_cost * dp_min / HUNDRED)),
            "max_amount": float(_money(system_cost * dp_max / HUNDRED)),
            "quick_add_amounts": [float(amount) for amount in settings.down_payment_quick_adds],
        },
        "subsidy": {"applied": bool(apply_subsidy), "amount": float(subsidy), "net_cost_after_subsidy": float(system_cost - subsidy)},
        "loan": {"amount": float(loan_amount)},
        "interest": {
            "rate": float(interest_rate),
            "basis_amount": float(rate_basis),
            "base_rate": float(base_rate),
            "min_rate": float(floor_rate),
            "is_locked": locked,
            "requested_rate": float(requested_rate) if requested_rate is not None else None,
            "rule_uid": rule.uid if rule else None,
            "rule_label": rule.label if rule else None,
            "unlock": unlock,
        },
        "tenure": {"years": tenure, "months": tenure * 12},
        "result": {
            "emi_per_month": emi["emi_per_month"],
            "total_payment": emi["total_payment"],
            "total_interest": emi["total_interest"],
            "daily_amount": daily_amount,
            "daily_saving_divisor": divisor,
            "monthly_savings": round(monthly_savings, 2),
        },
    }


def _with_legacy_keys(breakdown: dict) -> dict:
    """The headline numbers flattened beside the nested breakdown (older callers read them at the top level)."""
    system, subsidy, loan = breakdown["system"], breakdown["subsidy"], breakdown["loan"]
    interest, tenure, result = breakdown["interest"], breakdown["tenure"], breakdown["result"]
    return {
        **breakdown,
        "power_capacity_kW": system["capacity_kw"],
        "total_cost": system["system_cost"],
        "total_subsidy": subsidy["amount"],
        "final_cost": subsidy["net_cost_after_subsidy"],
        "principal": loan["amount"],
        "interest_rate": interest["rate"],
        "interest_rate_min": interest["min_rate"],
        "interest_rate_locked": interest["is_locked"],
        "tenure_years": tenure["years"],
        "tenure_months": tenure["months"],
        "emi_per_month": result["emi_per_month"],
        "total_payment": result["total_payment"],
        "total_interest": result["total_interest"],
        "daily_amount": result["daily_amount"],
    }


# ── POST calculators/emi/quotation/ ───────────────────────────────────────────────────────────────────────────────
def quotation(body, config: EmiConfig) -> dict:
    """``EMIQuotationAPIView.post``: the calculator's policy applied to up to six quotation package prices."""
    return _run(lambda: _quotation_view(body, config))


def _quotation_view(request_data, config: EmiConfig) -> dict:
    data = request_data or {}
    packages = data.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise EmiError("invalid_packages", "packages must be a non-empty object")
    if len(packages) > MAX_QUOTATION_PACKAGES:
        raise EmiError("too_many_packages", f"At most {MAX_QUOTATION_PACKAGES} packages")
    settings = config.settings
    try:
        capacity_kw = _to_float(data.get("capacity_kw"))
        tenure = _to_int(data.get("tenure_years"))
    except (TypeError, ValueError):
        raise EmiError("invalid_number", "Invalid numeric value in request") from None
    if capacity_kw is None or capacity_kw <= 0:
        raise EmiError("capacity_required", "capacity_kw is required")
    tenure = QUOTATION_DEFAULT_TENURE_YEARS if tenure is None else tenure
    if not settings.tenure_min_years <= tenure <= settings.tenure_max_years:
        raise EmiError("invalid_tenure", f"tenure_years must be between {settings.tenure_min_years} and {settings.tenure_max_years}")
    result = {}
    for key, package in packages.items():
        if not isinstance(package, dict):
            raise EmiError("invalid_package", f"packages.{key} must be an object")
        try:
            result[str(key)[:32]] = quotation_breakdown(
                config,
                capacity_kw=capacity_kw,
                system_cost=_to_float(package.get("system_cost")),
                subsidy=_to_float(package.get("subsidy")) or 0,
                tenure_years=tenure,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise EmiError("invalid_package", f"packages.{key}: {exc}") from None
    return {"tenure_years": tenure, "packages": result}


def quotation_breakdown(config: EmiConfig, capacity_kw, system_cost, subsidy, tenure_years) -> dict:
    """The calculator's policy for a price the quotation already knows: minimum down payment, rate band on price −
    down payment, EMI on price − down payment − subsidy."""
    settings = config.settings
    capacity = _dec(capacity_kw)
    system_cost = _money(_dec(system_cost))
    if system_cost <= 0:
        raise ValueError("system_cost must be greater than zero")
    subsidy = min(max(_money(_dec(subsidy, Decimal("0"))), Decimal("0")), system_cost)
    down_payment_percent = Decimal(settings.down_payment_min_percent)
    down_payment_amount = _money(system_cost * down_payment_percent / HUNDRED)
    rate_basis = _money(max(Decimal("0"), system_cost - down_payment_amount))
    loan_amount = _money(max(Decimal("0"), rate_basis - subsidy))
    rate, _base, _floor, _locked, rule = resolve_rate(config, capacity, rate_basis, system_cost)
    emi = emi_with_interest(principal=float(loan_amount), interest_rate=float(rate), tenure_years=tenure_years)
    divisor = settings.daily_saving_divisor or 30
    return {
        "system_cost": float(system_cost),
        "down_payment_percent": float(down_payment_percent),
        "down_payment": float(down_payment_amount),
        "subsidy": float(subsidy),
        "rate_basis": float(rate_basis),
        "loan_amount": float(loan_amount),
        "interest_rate": float(rate),
        "rule_label": rule.label if rule else None,
        "tenure_years": tenure_years,
        "emi_per_month": emi["emi_per_month"],
        "daily_amount": round(emi["emi_per_month"] / divisor, 2),
        "daily_saving_divisor": divisor,
    }


__all__: Sequence[str] = (
    "EmiConfig",
    "EmiError",
    "EmiSettings",
    "InterestRule",
    "SubsidyRule",
    "SystemSize",
    "breakdown_for",
    "calculate",
    "find_system_size",
    "quotation",
    "quotation_breakdown",
    "resolve_interest_rule",
    "resolve_rate",
    "resolve_rate_unlock",
    "resolve_subsidy",
)
