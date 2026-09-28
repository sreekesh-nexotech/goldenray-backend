"""Money: the one rounding rule and the GST composition rule (Flarize ``src/lib/money.js``, ``money.1``).

Every amount in ``engines/`` is a :class:`~decimal.Decimal`. Floats are refused at the boundary (:func:`to_decimal`
raises ``TypeError``): a caller parses JSON with ``parse_float=Decimal`` or passes strings/ints. The only binary
floating point anywhere in the core engines is the private replica of the JavaScript bill→units search in
:mod:`engines.energy` (its output is a whole number of units; see ``docs/decisions/engines-core.md``).

Rules ported from ``money.js`` (spec §1):

* compute at full precision, round **once**, when an amount is published (:func:`round_money`: half away from zero
  to whole rupees; symmetric for negatives);
* where published parts sit side by side (goods GST + service GST = total GST; base + GST = total) the total is the
  sum of the **rounded** parts (:func:`publish_parts`, :func:`apply_gst`), so a printed document reconciles to the
  rupee.

The four customer engines (energy, savings, subsidy, finance) predate ``money.js`` and round with JavaScript's
``Math.round`` — nearest integer, ties toward +∞ — which differs from :func:`round_money` only for negative halves
(``Math.round(-2.5) == -2``). :func:`js_round` is that rule; the engines use it wherever the JavaScript does.

GST composition (C73, PLAN §2.3 ``gst_goods_share``/``gst_services_share``/``gst_goods_rate``/``gst_services_rate``)
is ported from ``pricingEngine.resolveGstRegime``/``applyGst``: the effective rate (8.9 % for 70 % goods at 5 % + 30 %
services at 18 %) is derived and only cross-checked; the tax is computed per component and the published total is the
sum of the published components.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping, TypeVar

CURRENCY = "INR"
ROUNDING_MODE = "HALF_UP"
ROUNDING_UNIT = 1
MONEY_RULE_VERSION = "money.1"

#: Significant digits of every engine computation. Inputs carry a handful of places, so sums and products are exact;
#: divisions and powers (EMI) are carried ~45 digits beyond the rupee — the JavaScript works to ~16.
PRECISION = 60
CONTEXT = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])

ZERO = Decimal(0)
ONE = Decimal(1)
HALF = Decimal("0.5")
HUNDRED = Decimal(100)

F = TypeVar("F", bound=Callable[..., Any])


def exact(fn: F) -> F:
    """Run ``fn`` under :data:`CONTEXT` (60 digits, traps on invalid operations), whatever the caller's context."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with localcontext(CONTEXT):
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def to_decimal(value: Any, *, field: str = "value") -> Decimal:
    """``value`` as a finite :class:`Decimal`. Accepts ``Decimal``, ``int`` and numeric strings.

    ``float`` and ``bool`` raise ``TypeError`` (no binary floats in money paths); anything unparsable or non-finite
    raises ``ValueError``.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field}: {type(value).__name__} is not accepted in an engine; pass a Decimal, an int or a numeric string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{field}: {value!r} is not a number") from None
    else:
        raise TypeError(f"{field}: {type(value).__name__} is not a number")
    if not result.is_finite():
        raise ValueError(f"{field}: {value!r} is not finite")
    return result


def optional_decimal(value: Any, *, field: str = "value") -> Decimal | None:
    """:func:`to_decimal`, with ``None`` passed through."""
    return None if value is None else to_decimal(value, field=field)


def json_number(value: Any) -> Decimal | None:
    """A JSON number as a Decimal — the parsing boundary for legacy configuration files.

    A number read by ``json.loads`` without ``parse_float=Decimal`` arrives as a float; its shortest ``repr`` is the
    decimal the JSON text (and JavaScript) meant (``3.35``, not ``3.35000000000000008882…``). Non-numbers → ``None``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        return Decimal(repr(value)) if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (int, Decimal)):
        return Decimal(value) if Decimal(value).is_finite() else None
    return None


def is_number(value: Any) -> bool:
    """JavaScript ``typeof v === 'number' && isFinite(v)`` for engine inputs: a finite Decimal or an int (not bool)."""
    return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, Decimal) and value.is_finite())


def _refuse_float(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("float is not accepted in an engine; pass a Decimal, an int or a numeric string")


def js_number(value: Any) -> Decimal | None:
    """JavaScript ``Number(v)`` for engine inputs, ``None`` where it is ``NaN``/±Infinity. Floats raise.

    ``None`` → 0, ``True``/``False`` → 1/0, a string is trimmed and ``''`` → 0 (as in JavaScript).
    """
    _refuse_float(value)
    if value is None:
        return ZERO
    if isinstance(value, bool):
        return ONE if value else ZERO
    if isinstance(value, str) and not value.strip():
        return ZERO
    try:
        return to_decimal(value)
    except (TypeError, ValueError):
        return None


def is_money(value: Any) -> bool:
    """``money.js isMoney``: ``v != null && v !== '' && isFinite(Number(v))``. Floats raise."""
    _refuse_float(value)
    if value is None or value == "":
        return False
    return js_number(value) is not None


def _plain_zero(value: Decimal) -> Decimal:
    return ZERO if value == 0 else value


def round_money(value: Any) -> Decimal | None:
    """``money.js roundMoney``: whole rupees, half away from zero (``2.5 → 3``, ``-2.5 → -3``).

    ``None`` and non-numbers give ``None`` (the JavaScript's ``null``); floats raise ``TypeError``. The JavaScript adds
    ``Number.EPSILON × |v|`` before ``Math.round`` to undo binary representation error; decimal inputs have none, so
    the rule is exact half-up (for inputs of at most 15 significant digits the two agree).
    """
    if value is None:
        return None
    number = js_number(value)
    if number is None:
        return None
    with localcontext(CONTEXT):
        return _plain_zero(number.quantize(ONE, rounding=ROUND_HALF_UP))


def js_round(value: Decimal | int) -> Decimal:
    """JavaScript ``Math.round``: the nearest integer, ties toward +∞ (``2.5 → 3``, ``-2.5 → -2``)."""
    with localcontext(CONTEXT):
        return _plain_zero((Decimal(value) + HALF).to_integral_value(rounding=ROUND_FLOOR))


def round_places(value: Decimal | int, places: int) -> Decimal:
    """``Math.round(v × 10^p) / 10^p`` — the engines' one-decimal and two-decimal figures."""
    with localcontext(CONTEXT):
        scale = Decimal(10) ** places
        return _plain_zero(js_round(Decimal(value) * scale) / scale)


def _js_number(value: Any) -> Decimal:
    """``Number(v) || 0`` for the exact helpers: non-numbers count as zero (floats raise)."""
    number = js_number(value)
    return ZERO if number is None else number


@exact
def sum_exact(values: Iterable[Any] | None) -> Decimal:
    """``money.js sumExact``: the exact sum; entries that are not numbers count as 0. Round once, afterwards."""
    return sum((_js_number(value) for value in (values or ())), ZERO)


@exact
def mul_exact(a: Any, b: Any) -> Decimal:
    """``money.js mulExact``: the exact product; non-numbers count as 0."""
    return _js_number(a) * _js_number(b)


def publish_parts(parts: Iterable[Any]) -> tuple[tuple[Decimal | None, ...], Decimal]:
    """The composition rule: round each published part, and total the **rounded** parts (``None`` parts count 0)."""
    rounded = tuple(round_money(part) for part in parts)
    return rounded, sum((part for part in rounded if part is not None), ZERO)


def canonical(value: Decimal | int) -> str:
    """The plain decimal string of a number (``Decimal('5.0E+3') → '5000'``, ``Decimal('7.90') → '7.9'``, ``-0 → '0'``).

    Used for payload text and for the parity comparison with the JavaScript's JSON numbers.
    """
    number = Decimal(value)
    if number == 0:
        return "0"
    # a context as wide as the coefficient: stripping trailing zeros never rounds, whatever the caller's precision
    return format(number.normalize(Context(prec=max(len(number.as_tuple().digits), 1))), "f")


def js_text(value: Any) -> str:
    """How a JavaScript template literal prints an engine input (``${v}``): numbers plainly, ``None`` as ``null``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return canonical(value) if Decimal(value).is_finite() else ("NaN" if Decimal(value).is_nan() else ("Infinity" if value > 0 else "-Infinity"))
    return str(value)


# ---- GST composition (C73) --------------------------------------------------------------------------------------


class GstRegimeCode(StrEnum):
    SOLAR_70_30_COMPOSITE = "SOLAR_70_30_COMPOSITE"
    FLAT = "FLAT"


class GstError(StrEnum):
    """``pricingEngine`` error codes of the GST resolution."""

    GST_NOT_CONFIGURED = "GST_NOT_CONFIGURED"
    GST_REGIME_INVALID = "GST_REGIME_INVALID"
    GST_SPLIT_INVALID = "GST_SPLIT_INVALID"
    GST_EFFECTIVE_RATE_MISMATCH = "GST_EFFECTIVE_RATE_MISMATCH"


class GstConfigError(ValueError):
    """A GST configuration that cannot be resolved; ``code`` is a :class:`GstError`."""

    def __init__(self, code: GstError, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


#: Tolerance of the valuation split (``|goods + service − 100| > 1e-9`` fails).
SPLIT_TOLERANCE = Decimal("1E-9")
#: Tolerance of a declared effective rate against the derived one.
EFFECTIVE_RATE_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True)
class GstConfig:
    """A GST configuration as the Flarize ``cost-config.json`` ``gst`` block states it (percentages, not fractions).

    ``regime`` ``None`` with a ``rate_pct`` means FLAT (the JavaScript default). :meth:`from_cost_config` builds one
    from the PLAN §2.3 ``pricing_cost_config`` keys, which store shares and rates as fractions.
    """

    regime: str | None = None
    rate_pct: Decimal | None = None
    goods_valuation_pct: Decimal | None = None
    goods_rate_pct: Decimal | None = None
    service_valuation_pct: Decimal | None = None
    service_rate_pct: Decimal | None = None
    effective_rate_pct: Decimal | None = None

    @classmethod
    def from_cost_config(cls, *, gst_goods_share: Any, gst_goods_rate: Any, gst_services_share: Any, gst_services_rate: Any) -> GstConfig:
        """The composite regime from the four ``pricing_cost_config`` keys (fractions: ``0.70``, ``0.05``, ``0.30``, ``0.18``)."""
        with localcontext(CONTEXT):
            return cls(
                regime=GstRegimeCode.SOLAR_70_30_COMPOSITE,
                goods_valuation_pct=to_decimal(gst_goods_share, field="gst_goods_share") * HUNDRED,
                goods_rate_pct=to_decimal(gst_goods_rate, field="gst_goods_rate") * HUNDRED,
                service_valuation_pct=to_decimal(gst_services_share, field="gst_services_share") * HUNDRED,
                service_rate_pct=to_decimal(gst_services_rate, field="gst_services_rate") * HUNDRED,
            )

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> GstConfig:
        """The Flarize ``gst`` block (``regime``, ``ratePct``, ``goodsValuationPct``, …)."""
        data = data or {}
        return cls(
            regime=data.get("regime") or None,
            rate_pct=json_number(data.get("ratePct")),
            goods_valuation_pct=json_number(data.get("goodsValuationPct")),
            goods_rate_pct=json_number(data.get("goodsRatePct")),
            service_valuation_pct=json_number(data.get("serviceValuationPct")),
            service_rate_pct=json_number(data.get("serviceRatePct")),
            effective_rate_pct=json_number(data.get("effectiveRatePct")),
        )


@dataclass(frozen=True)
class GstComponent:
    label: str
    valuation_pct: Decimal
    rate_pct: Decimal


@dataclass(frozen=True)
class GstRegime:
    regime: GstRegimeCode
    effective_rate_pct: Decimal
    components: tuple[GstComponent, ...]


@dataclass(frozen=True)
class AppliedGstComponent:
    label: str
    valuation_pct: Decimal
    rate_pct: Decimal
    taxable_exact: Decimal
    tax_exact: Decimal
    taxable_value: Decimal
    tax_amount: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "valuationPct": self.valuation_pct, "ratePct": self.rate_pct, "taxableValue": self.taxable_value, "taxAmount": self.tax_amount}


@dataclass(frozen=True)
class GstApplication:
    """GST on one pre-GST amount: per-component published figures and the published total (sum of the rounded taxes)."""

    components: tuple[AppliedGstComponent, ...]
    total_published: Decimal
    total_exact: Decimal


@exact
def resolve_gst_regime(config: GstConfig) -> GstRegime:
    """``pricingEngine.resolveGstRegime``. Raises :class:`GstConfigError` — nothing is assumed."""
    regime = config.regime or (GstRegimeCode.FLAT if config.rate_pct is not None else None)
    if regime == GstRegimeCode.SOLAR_70_30_COMPOSITE:
        parts = (config.goods_valuation_pct, config.goods_rate_pct, config.service_valuation_pct, config.service_rate_pct)
        if any(part is None for part in parts):
            raise GstConfigError(GstError.GST_SPLIT_INVALID, "SOLAR_70_30_COMPOSITE requires goodsValuationPct, goodsRatePct, serviceValuationPct and serviceRatePct.")
        gv, gr, sv, sr = (to_decimal(part) for part in parts)
        if abs(gv + sv - HUNDRED) > SPLIT_TOLERANCE:
            raise GstConfigError(GstError.GST_SPLIT_INVALID, f"Valuation split must total 100% of the supply (got {canonical(gv)} + {canonical(sv)} = {canonical(gv + sv)}).")
        # Derived, rounded to 6 places for reporting; the tax is computed from the components, never from this.
        effective = js_round(((gv / HUNDRED) * gr + (sv / HUNDRED) * sr) * Decimal(1_000_000)) / Decimal(1_000_000)
        if config.effective_rate_pct is not None and abs(to_decimal(config.effective_rate_pct) - effective) > EFFECTIVE_RATE_TOLERANCE:
            raise GstConfigError(
                GstError.GST_EFFECTIVE_RATE_MISMATCH,
                f"Declared effective rate {canonical(config.effective_rate_pct)}% does not match the split "
                f"({canonical(gv)}% × {canonical(gr)}% + {canonical(sv)}% × {canonical(sr)}% = {canonical(effective)}%). The split is authoritative.",
            )
        return GstRegime(
            regime=GstRegimeCode.SOLAR_70_30_COMPOSITE,
            effective_rate_pct=_plain_zero(effective),
            components=(GstComponent("GOODS", gv, gr), GstComponent("SERVICE", sv, sr)),
        )
    if regime == GstRegimeCode.FLAT:
        if config.rate_pct is None:
            raise GstConfigError(GstError.GST_NOT_CONFIGURED, "FLAT regime requires ratePct.")
        rate = to_decimal(config.rate_pct)
        return GstRegime(regime=GstRegimeCode.FLAT, effective_rate_pct=rate, components=(GstComponent("WHOLE_SUPPLY", HUNDRED, rate),))
    if not regime:
        raise GstConfigError(GstError.GST_NOT_CONFIGURED, "No GST configuration. No rate and no regime is assumed.")
    raise GstConfigError(GstError.GST_REGIME_INVALID, f'Unknown GST regime "{regime}".')


@exact
def apply_gst(regime: GstRegime, base: Any) -> GstApplication:
    """``pricingEngine.applyGst``: taxable = base × valuation %, tax = taxable × rate %, each published rounded; the
    published total is the sum of the published taxes, the exact total is kept for audit."""
    base_exact = to_decimal(base, field="base")
    applied = []
    for component in regime.components:
        taxable_exact = base_exact * (component.valuation_pct / HUNDRED)
        tax_exact = taxable_exact * (component.rate_pct / HUNDRED)
        applied.append(
            AppliedGstComponent(
                label=component.label,
                valuation_pct=component.valuation_pct,
                rate_pct=component.rate_pct,
                taxable_exact=taxable_exact,
                tax_exact=tax_exact,
                taxable_value=round_money(taxable_exact),
                tax_amount=round_money(tax_exact),
            )
        )
    return GstApplication(
        components=tuple(applied),
        total_published=sum((component.tax_amount for component in applied), ZERO),
        total_exact=sum((component.tax_exact for component in applied), ZERO),
    )
