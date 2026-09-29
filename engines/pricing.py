"""Gross-margin pricing — port of Flarize ``src/lib/pricingEngine.js`` (``pricingEngine.3``).

    listSellingPriceBeforeGST = totalActualProjectCost / (1 − m)      GROSS MARGIN, never markup (decision C60)
    discount (amount or % of list), 0 ≤ d ≤ list  →  sellingPriceBeforeGST
    actual gross margin below the tier minimum → REJECTED (no override); between minimum and target → Sales Head approval
    GST per regime component (SOLAR_70_30_COMPOSITE: 70 % goods @ 5 % + 30 % service @ 18 %); published GST = Σ rounded parts

Its only numeric input is ``costResult.totalActualProjectCostExact`` from :func:`engines.cost.calculate_cost`.
``MARKUP`` is forbidden: :func:`calculate_pricing` returns ``REJECTED``/``MARGIN_TYPE_FORBIDDEN`` exactly like the JS, and
:func:`gross_margin_list_price` — the single list-price formula other contexts call — raises :class:`PricingError`.
"""

from __future__ import annotations

from typing import Any

from engines._jscompat import JsError, clean, is_nullish, js_number, js_or, js_round, js_str, js_str_key, js_to_fixed, jsget, nullish, truthy
from engines._money_compat import is_money, round_money, sum_exact

PRICING_ENGINE_VERSION = "pricingEngine.3"
MARGIN_TYPE = {"GROSS_MARGIN": "GROSS_MARGIN"}
FORBIDDEN_MARGIN_TYPES = ("MARKUP",)
MONEY_SEMANTIC = {name: name for name in ("PURCHASE_COST", "INTERNAL_COST", "SELLING_PRICE", "CUSTOMER_EXTRA", "GST", "TOTAL_CUSTOMER_PRICE", "REFERENCE_ONLY")}
EXTRA_TYPE = {"OPTIONAL_ACCESSORY": "OPTIONAL_ACCESSORY", "ADDITIONAL_SERVICE": "ADDITIONAL_SERVICE", "OTHER_APPROVED_EXTRA": "OTHER_APPROVED_EXTRA"}
FORBIDDEN_EXTRA_TYPES = ("NET_METERING", "NET_METER")
CUSTOMER_SIDE_EXPENSE_TYPE = {"NET_METER": "NET_METER", "GOVERNMENT_FEE": "GOVERNMENT_FEE", "OTHER_CUSTOMER_PROCURED": "OTHER_CUSTOMER_PROCURED"}
GST_REGIME = {"SOLAR_70_30_COMPOSITE": "SOLAR_70_30_COMPOSITE", "FLAT": "FLAT"}
PRICING_ERROR = {
    name: name
    for name in (
        "COST_NOT_AVAILABLE",
        "COST_INCOMPLETE",
        "MARGIN_NOT_CONFIGURED",
        "MARGIN_OUT_OF_RANGE",
        "MARGIN_TYPE_FORBIDDEN",
        "GST_NOT_CONFIGURED",
        "GST_REGIME_INVALID",
        "GST_SPLIT_INVALID",
        "GST_EFFECTIVE_RATE_MISMATCH",
        "EXTRA_INVALID",
        "MARGIN_CONFIG_INVALID",
        "MARGIN_BELOW_MINIMUM",
        "DISCOUNT_INVALID",
        "EXTRA_TYPE_FORBIDDEN",
    )
}
APPROVAL = {"NONE_REQUIRED": "NONE_REQUIRED", "SALES_HEAD_APPROVAL_REQUIRED": "SALES_HEAD_APPROVAL_REQUIRED", "BLOCKED": "BLOCKED"}
TIERS = ("BASE", "VALUE", "PREMIUM")
PRICING_STATUS = {"COMPLETE": "COMPLETE", "REJECTED": "REJECTED"}

_COST_UNAVAILABLE = "UNAVAILABLE"
_COST_COMPLETE = "COMPLETE"
_TOLERANCE = 1e-9


class PricingError(JsError):
    """Raised by :func:`gross_margin_list_price` (MARKUP, out-of-range or unconfigured margin)."""


def gross_margin_list_price(cost: Any, margin: Any, *, margin_type: str = "GROSS_MARGIN") -> int | float:
    """``cost / (1 − margin)`` — the only list-price formula. ``MARKUP`` (``cost × (1 + m)``) raises; so does m ∉ [0, 1)."""
    if margin_type in FORBIDDEN_MARGIN_TYPES:
        raise PricingError(f'marginType "{margin_type}" is forbidden. The approved model is GROSS_MARGIN (C60).', PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"])
    if margin_type != MARGIN_TYPE["GROSS_MARGIN"]:
        raise PricingError(f'Unknown marginType "{js_str(margin_type)}". Only GROSS_MARGIN is supported.', PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"])
    checked = validate_gross_margin(margin)
    if not checked["valid"]:
        raise PricingError(checked["reason"], checked["code"])
    return js_number(cost) / (1 - checked["value"])


def resolve_tier_margin(margin_config: Any = None, tier: Any = None) -> dict:
    """Per-tier gross margin: ``marginType`` GROSS_MARGIN, tier in BASE/VALUE/PREMIUM, 0 ≤ minimum ≤ target < 100."""
    config = margin_config if isinstance(margin_config, dict) else {}
    margin_type = jsget(config, "marginType")
    if truthy(margin_type) and margin_type in FORBIDDEN_MARGIN_TYPES:
        return {
            "ok": False,
            "code": PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"],
            "reason": f'marginType "{js_str(margin_type)}" is forbidden. The approved model is GROSS_MARGIN (C60).',
        }
    if truthy(margin_type) and margin_type != MARGIN_TYPE["GROSS_MARGIN"]:
        return {"ok": False, "code": PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"], "reason": f'Unknown marginType "{js_str(margin_type)}". Only GROSS_MARGIN is supported.'}
    key = js_str(js_or(tier, "")).upper()
    if key not in TIERS:
        return {
            "ok": False,
            "code": PRICING_ERROR["MARGIN_CONFIG_INVALID"],
            "reason": f'Tier "{js_str(nullish(tier, "undeclared"))}" is not one of {", ".join(TIERS)}. Margin is tier-specific.',
        }
    tiers = js_or(jsget(config, "tiers"), {})
    entry = jsget(tiers, key)
    if not truthy(entry) or not is_money(jsget(entry, "targetMarginPct")) or not is_money(jsget(entry, "minimumMarginPct")):
        return {
            "ok": False,
            "code": PRICING_ERROR["MARGIN_CONFIG_INVALID"],
            "reason": f"Tier {key} has no approved targetMarginPct / minimumMarginPct. No percentage is assumed.",
            "detail": {"tier": key},
        }
    target = js_number(entry["targetMarginPct"])
    minimum = js_number(entry["minimumMarginPct"])
    if target < 0 or target >= 100:
        return {"ok": False, "code": PRICING_ERROR["MARGIN_CONFIG_INVALID"], "reason": f"Tier {key} targetMarginPct {js_str(target)} is out of range (0 <= target < 100)."}
    if minimum < 0:
        return {"ok": False, "code": PRICING_ERROR["MARGIN_CONFIG_INVALID"], "reason": f"Tier {key} minimumMarginPct {js_str(minimum)} is negative."}
    if minimum > target:
        return {
            "ok": False,
            "code": PRICING_ERROR["MARGIN_CONFIG_INVALID"],
            "reason": f"Tier {key} minimumMarginPct {js_str(minimum)} exceeds targetMarginPct {js_str(target)}. The constraint is 0 <= minimum <= target < 100.",
        }
    return {
        "ok": True,
        "tier": key,
        "targetMarginPct": target,
        "minimumMarginPct": minimum,
        "targetGrossMargin": target / 100,
        "minimumGrossMargin": minimum / 100,
        "version": nullish(jsget(config, "version"), None),
    }


def resolve_gst_regime(gst: Any = None) -> dict:
    """Resolve a GST configuration into components summing to the whole supply; nothing is assumed."""
    gst = gst if isinstance(gst, dict) else {}
    regime = js_or(jsget(gst, "regime"), GST_REGIME["FLAT"] if is_money(jsget(gst, "ratePct")) else None)
    if regime == GST_REGIME["SOLAR_70_30_COMPOSITE"]:
        gv, gr, sv, sr = (js_number(jsget(gst, k)) for k in ("goodsValuationPct", "goodsRatePct", "serviceValuationPct", "serviceRatePct"))
        if not all(abs(x) != float("inf") and x == x for x in (gv, gr, sv, sr)):
            return {
                "ok": False,
                "code": PRICING_ERROR["GST_SPLIT_INVALID"],
                "reason": "SOLAR_70_30_COMPOSITE requires goodsValuationPct, goodsRatePct, serviceValuationPct and serviceRatePct.",
            }
        if abs(gv + sv - 100) > _TOLERANCE:
            return {
                "ok": False,
                "code": PRICING_ERROR["GST_SPLIT_INVALID"],
                "reason": f"Valuation split must total 100% of the supply (got {js_str(gv)} + {js_str(sv)} = {js_str(gv + sv)}).",
            }
        effective = js_round(((gv / 100) * gr + (sv / 100) * sr) * 1e6) / 1e6
        declared = jsget(gst, "effectiveRatePct")
        if is_money(declared) and abs(js_number(declared) - effective) > 0.0001:
            return {
                "ok": False,
                "code": PRICING_ERROR["GST_EFFECTIVE_RATE_MISMATCH"],
                "reason": (
                    f"Declared effective rate {js_str(declared)}% does not match the split "
                    f"({js_str(gv)}% × {js_str(gr)}% + {js_str(sv)}% × {js_str(sr)}% = {js_str(effective)}%). The split is authoritative."
                ),
            }
        return {
            "ok": True,
            "regime": regime,
            "effectiveRatePct": effective,
            "components": [{"label": "GOODS", "valuationPct": gv, "ratePct": gr}, {"label": "SERVICE", "valuationPct": sv, "ratePct": sr}],
        }
    if regime == GST_REGIME["FLAT"]:
        if not is_money(jsget(gst, "ratePct")):
            return {"ok": False, "code": PRICING_ERROR["GST_NOT_CONFIGURED"], "reason": "FLAT regime requires ratePct."}
        rate = js_number(gst["ratePct"])
        return {"ok": True, "regime": regime, "effectiveRatePct": rate, "components": [{"label": "WHOLE_SUPPLY", "valuationPct": 100, "ratePct": rate}]}
    if not truthy(regime):
        return {"ok": False, "code": PRICING_ERROR["GST_NOT_CONFIGURED"], "reason": "No GST configuration. No rate and no regime is assumed."}
    return {"ok": False, "code": PRICING_ERROR["GST_REGIME_INVALID"], "reason": f'Unknown GST regime "{js_str(regime)}".'}


def _apply_gst(resolved: dict, base_exact: Any) -> dict:
    components = []
    for component in resolved["components"]:
        taxable_exact = base_exact * (component["valuationPct"] / 100)
        tax_exact = taxable_exact * (component["ratePct"] / 100)
        components.append({**component, "taxableExact": taxable_exact, "taxExact": tax_exact, "taxableValue": round_money(taxable_exact), "taxAmount": round_money(tax_exact)})
    return {"components": components, "totalPublished": sum_exact(c["taxAmount"] for c in components), "totalExact": sum_exact(c["taxExact"] for c in components)}


def validate_gross_margin(margin: Any) -> dict:
    """0 ≤ margin < 1: 100 % and above is rejected (the price would be undefined), and so is a negative margin."""
    if not is_money(margin):
        return {"valid": False, "code": PRICING_ERROR["MARGIN_NOT_CONFIGURED"], "reason": "No approved target gross margin is configured. None is assumed."}
    value = js_number(margin)
    if value < 0:
        return {"valid": False, "code": PRICING_ERROR["MARGIN_OUT_OF_RANGE"], "reason": f"Gross margin {js_str(value)} is negative."}
    if value >= 1:
        return {
            "valid": False,
            "code": PRICING_ERROR["MARGIN_OUT_OF_RANGE"],
            "reason": f"Gross margin {js_str(value)} is 100% or more. 1 - margin would be zero or negative, so the price is undefined. Rejected.",
        }
    return {"valid": True, "value": value}


def _sorted_by(items: Any, key: str) -> list:
    return sorted(list(items or []), key=lambda item: js_str_key(js_str(js_or(jsget(item, key), ""))))


def _price_extras(extras: Any, gst_resolved: dict, errors: list) -> list:
    priced = []
    for extra in _sorted_by(extras, "extraId"):
        extra_type = jsget(extra, "type")
        if extra_type in FORBIDDEN_EXTRA_TYPES:
            errors.append(
                {
                    "code": PRICING_ERROR["EXTRA_TYPE_FORBIDDEN"],
                    "message": (
                        f'"{js_str(extra_type)}" cannot be a Flarize extra. The net meter is purchased by the customer as KSEB requires; '
                        "it is not a Flarize item and is never marked up. Record it under customerSideExpenses instead."
                    ),
                    "detail": extra,
                }
            )
            continue
        if not truthy(jsget(extra, "extraId")) or not is_money(jsget(extra, "customerPrice")):
            errors.append(
                {
                    "code": PRICING_ERROR["EXTRA_INVALID"],
                    "message": f'Extra "{js_str(nullish(jsget(extra, "extraId"), "(no id)"))}" is missing an id or a customer price. Not priced.',
                    "detail": extra,
                }
            )
            continue
        own_rate = js_number(extra["gstRatePct"]) if is_money(jsget(extra, "gstRatePct")) else None
        ex_gst = js_number(extra["customerPrice"])
        if own_rate is None:
            resolved = gst_resolved
        else:
            resolved = {"ok": True, "regime": GST_REGIME["FLAT"], "effectiveRatePct": own_rate, "components": [{"label": "WHOLE_SUPPLY", "valuationPct": 100, "ratePct": own_rate}]}
        applied = _apply_gst(resolved, ex_gst)
        published = applied["totalPublished"]
        priced.append(
            {
                "extraId": extra["extraId"],
                "type": js_or(extra_type, EXTRA_TYPE["OTHER_APPROVED_EXTRA"]),
                "description": nullish(jsget(extra, "description"), None),
                "includedInBase": False,
                "semantic": MONEY_SEMANTIC["CUSTOMER_EXTRA"],
                "customerPriceExcludingGST": round_money(ex_gst),
                "gstRegime": resolved["regime"],
                "gstRatePct": resolved["effectiveRatePct"],
                "gstComponents": [
                    {"label": c["label"], "valuationPct": c["valuationPct"], "ratePct": c["ratePct"], "taxableValue": c["taxableValue"], "taxAmount": c["taxAmount"]} for c in applied["components"]
                ],
                "gstAmount": round_money(published),
                "customerPriceIncludingGST": round_money(ex_gst) + published,
                "exactExGst": ex_gst,
                "exactGst": applied["totalExact"],
                "publishedGst": published,
            }
        )
    return priced


def _customer_side(expenses: Any) -> list:
    return [
        {
            "expenseId": x["expenseId"],
            "type": js_or(jsget(x, "type"), CUSTOMER_SIDE_EXPENSE_TYPE["OTHER_CUSTOMER_PROCURED"]),
            "description": nullish(jsget(x, "description"), None),
            "amount": round_money(js_number(x["amount"])),
            "paidBy": "CUSTOMER",
            "flarizeMarkup": 0,
            "includedInFlarizePrice": False,
            "semantic": MONEY_SEMANTIC["REFERENCE_ONLY"],
            "note": "Customer-procured. Not in the Flarize BOM, cost, margin or price. Never marked up.",
        }
        for x in _sorted_by(expenses, "expenseId")
        if truthy(jsget(x, "expenseId")) and is_money(jsget(x, "amount"))
    ]


def _resolve_margin(cost_result: dict, margin: dict, tier: Any, reject) -> tuple[Any, Any]:
    if truthy(jsget(margin, "tiers")):
        tier_margin = resolve_tier_margin(margin, nullish(tier, jsget(cost_result, "tier")))
        if not tier_margin["ok"]:
            return None, reject(tier_margin["code"], tier_margin["reason"], nullish(jsget(tier_margin, "detail"), None))
        return tier_margin, {"valid": True, "value": tier_margin["targetGrossMargin"]}
    margin_type = jsget(margin, "marginType")
    if truthy(margin_type) and margin_type in FORBIDDEN_MARGIN_TYPES:
        return None, reject(PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"], f'marginType "{js_str(margin_type)}" is forbidden in V1. The approved model is GROSS_MARGIN (C60).')
    if truthy(margin_type) and margin_type != MARGIN_TYPE["GROSS_MARGIN"]:
        return None, reject(PRICING_ERROR["MARGIN_TYPE_FORBIDDEN"], f'Unknown marginType "{js_str(margin_type)}". Only GROSS_MARGIN is supported.')
    checked = validate_gross_margin(jsget(margin, "targetGrossMargin"))
    if not checked["valid"]:
        return None, reject(checked["code"], checked["reason"], {"targetGrossMargin": nullish(jsget(margin, "targetGrossMargin"), None)})
    return None, checked


def calculate_pricing(
    *,
    cost_result: Any,
    margin: Any = None,
    gst: Any = None,
    extras: Any = None,
    market_rate: Any = None,
    priced_at: Any = None,
    tier: Any = None,
    discount: Any = None,
    customer_side_expenses: Any = None,
) -> dict:
    """``calculatePricing`` → ``COMPLETE`` (list, discount, margin gate, GST components, extras, totals) or ``REJECTED``."""
    margin = margin if isinstance(margin, dict) else {}
    gst = gst if isinstance(gst, dict) else {}
    errors: list[dict] = []

    def reject(code: str, message: str, detail: Any = None) -> dict:
        errors.append({"code": code, "message": message, "detail": nullish(detail, None)})
        return {
            "status": PRICING_STATUS["REJECTED"],
            "error": code,
            "errors": errors,
            "pricingEngineVersion": PRICING_ENGINE_VERSION,
            "pricedAt": priced_at,
            "costResultId": nullish(jsget(cost_result, "bomSnapshotId"), None),
            "sellingPriceBeforeGST": None,
            "gstAmount": None,
            "sellingPriceIncludingGST": None,
        }

    if not truthy(cost_result) or jsget(cost_result, "status") == _COST_UNAVAILABLE:
        return reject(PRICING_ERROR["COST_NOT_AVAILABLE"], "No cost result. The BOM must be locked and costed before it can be priced.")
    cost_exact = nullish(jsget(cost_result, "totalActualProjectCostExact"), jsget(cost_result, "totalActualCostExact"))
    if jsget(cost_result, "status") != _COST_COMPLETE or is_nullish(cost_exact):
        cost_errors = jsget(cost_result, "errors")
        return reject(
            PRICING_ERROR["COST_INCOMPLETE"],
            "Cost is incomplete. A price computed from a partial cost would understate the true cost.",
            [jsget(e, "code") for e in cost_errors] if isinstance(cost_errors, list) else None,
        )
    tier_margin, checked = _resolve_margin(cost_result, margin, tier, reject)
    if checked.get("status") == PRICING_STATUS["REJECTED"]:
        return checked
    gst_resolved = resolve_gst_regime(gst)
    if not gst_resolved["ok"]:
        return reject(gst_resolved["code"], gst_resolved["reason"], {"regime": nullish(jsget(gst, "regime"), None)})

    cost = cost_exact
    list_exact = cost / (1 - checked["value"])
    discount_exact: int | float = 0
    discount_detail = None
    if truthy(discount):
        if is_money(jsget(discount, "amount")):
            discount_exact = js_number(discount["amount"])
        elif is_money(jsget(discount, "percentage")):
            discount_exact = list_exact * (js_number(discount["percentage"]) / 100)
        else:
            return reject(PRICING_ERROR["DISCOUNT_INVALID"], "A discount must declare either an amount or a percentage.", discount)
        if discount_exact < 0:
            return reject(PRICING_ERROR["DISCOUNT_INVALID"], "A discount cannot be negative.", discount)
        if discount_exact > list_exact:
            return reject(PRICING_ERROR["DISCOUNT_INVALID"], "A discount cannot exceed the list selling price.", discount)
        discount_detail = {
            "amount": round_money(discount_exact),
            "percentage": nullish(jsget(discount, "percentage"), None),
            "requestedBy": nullish(jsget(discount, "requestedBy"), None),
            "reason": nullish(jsget(discount, "reason"), None),
        }
    selling_exact = list_exact - discount_exact
    gross_profit_exact = selling_exact - cost
    actual_pct = None if selling_exact == 0 else (gross_profit_exact / selling_exact) * 100

    approval, approval_reason = APPROVAL["NONE_REQUIRED"], None
    if tier_margin is not None:
        minimum, target = tier_margin["minimumMarginPct"], tier_margin["targetMarginPct"]
        if actual_pct is None or actual_pct < minimum - _TOLERANCE:
            shown = "undefined" if actual_pct is None else js_to_fixed(actual_pct, 4)
            return reject(
                PRICING_ERROR["MARGIN_BELOW_MINIMUM"],
                f"Actual gross margin {shown}% is below the {js_str(minimum)}% minimum for tier {tier_margin['tier']}. The quotation is BLOCKED. "
                "Sales cannot bypass this gate; no override workflow exists in V1.",
                {"tier": tier_margin["tier"], "actualGrossMarginPct": actual_pct, "minimumMarginPct": minimum, "targetMarginPct": target},
            )
        if actual_pct < target - _TOLERANCE:
            approval = APPROVAL["SALES_HEAD_APPROVAL_REQUIRED"]
            approval_reason = (
                f"Actual gross margin {js_to_fixed(actual_pct, 4)}% is below the {js_str(target)}% target but at or above the "
                f"{js_str(minimum)}% minimum for tier {tier_margin['tier']}. Sales Head approval is required."
            )

    applied = _apply_gst(gst_resolved, selling_exact)
    selling_published = round_money(selling_exact)
    gst_published = applied["totalPublished"]
    priced_extras = _price_extras(extras, gst_resolved, errors)
    extras_ex_gst = sum_exact(e["customerPriceExcludingGST"] for e in priced_extras)
    extras_gst = sum_exact(e["publishedGst"] for e in priced_extras)
    customer_side = _customer_side(customer_side_expenses)
    actual_published = None if actual_pct is None else round_money(actual_pct * 10000) / 10000
    total_cost = nullish(jsget(cost_result, "totalActualProjectCost"), jsget(cost_result, "totalActualCost"))
    return clean(
        {
            "status": PRICING_STATUS["COMPLETE"],
            "error": None,
            "errors": errors,
            "costResultId": jsget(cost_result, "bomSnapshotId"),
            "projectId": jsget(cost_result, "projectId"),
            "pricingEngineVersion": PRICING_ENGINE_VERSION,
            "costEngineVersion": jsget(cost_result, "costEngineVersion"),
            "marginVersion": nullish(jsget(tier_margin, "version"), jsget(margin, "version"), None),
            "gstVersion": nullish(jsget(gst, "version"), None),
            "pricedAt": priced_at,
            "totalActualProjectCost": total_cost,
            "totalActualCost": total_cost,
            "tier": nullish(jsget(tier_margin, "tier"), tier, jsget(cost_result, "tier"), None),
            "marginType": MARGIN_TYPE["GROSS_MARGIN"],
            "targetGrossMargin": checked["value"],
            "targetMarginPct": nullish(jsget(tier_margin, "targetMarginPct"), round_money(checked["value"] * 10000) / 100),
            "minimumMarginPct": nullish(jsget(tier_margin, "minimumMarginPct"), None),
            "marginFormula": "listSellingPriceBeforeGST = totalActualProjectCost ÷ (1 − targetGrossMargin)",
            "listSellingPriceBeforeGST": round_money(list_exact),
            "discount": discount_detail,
            "discountAmount": round_money(discount_exact),
            "sellingPriceBeforeGST": round_money(selling_exact),
            "finalSellingPriceBeforeGST": round_money(selling_exact),
            "grossProfit": round_money(gross_profit_exact),
            "actualGrossMarginPct": actual_published,
            "achievedGrossMarginPct": None if actual_pct is None else round_money(actual_pct * 100) / 100,
            "approval": approval,
            "approvalReason": approval_reason,
            "gstRegime": gst_resolved["regime"],
            "gstRatePct": gst_resolved["effectiveRatePct"],
            "gstEffectiveRatePct": gst_resolved["effectiveRatePct"],
            "gstComponents": [
                {
                    "label": c["label"],
                    "valuationPct": c["valuationPct"],
                    "ratePct": c["ratePct"],
                    "taxableValue": c["taxableValue"],
                    "taxAmount": c["taxAmount"],
                    "formula": f"{js_str(round_money(c['taxableExact']))} × {js_str(c['ratePct'])}%",
                }
                for c in applied["components"]
            ],
            "gstAppliesTo": js_or(jsget(gst, "appliesTo"), "SELLING_PRICE_BEFORE_GST"),
            "priceExcludingGST": selling_published,
            "gstAmount": gst_published,
            "priceIncludingGST": selling_published + gst_published,
            "sellingPriceIncludingGST": selling_published + gst_published,
            "extras": priced_extras,
            "extrasExcludingGST": extras_ex_gst,
            "extrasGstAmount": extras_gst,
            "extrasIncludingGST": extras_ex_gst + extras_gst,
            "customerTotalIncludingGST": selling_published + gst_published + extras_ex_gst + extras_gst,
            "customerSideExpenses": customer_side,
            "customerSideExpensesTotal": sum_exact(x["amount"] for x in customer_side),
            "discountLayer": {
                "listSellingPriceBeforeGST": round_money(list_exact),
                "approvedDiscount": discount_detail,
                "netSellingPriceBeforeGST": round_money(selling_exact),
                "actualGrossMarginPct": actual_published,
                "minimumMarginPct": nullish(jsget(tier_margin, "minimumMarginPct"), None),
                "approval": approval,
                "note": (
                    "A discount is applied between the list price and GST. It NEVER modifies the underlying cost, "
                    "and the actual gross margin is always recomputed after it. The Sales discount UI is a later phase."
                ),
            },
            "marketRateReference": (
                None
                if is_nullish(market_rate)
                else {
                    "value": market_rate,
                    "semantic": MONEY_SEMANTIC["REFERENCE_ONLY"],
                    "usedInCalculation": False,
                    "varianceVsSellingPrice": round_money(js_number(market_rate) - selling_exact),
                    "note": "Benchmark only. It does not replace TOTAL_ACTUAL_COST and does not replace the Pricing Engine.",
                }
            ),
            "exact": {"sellingBeforeGstExact": selling_exact, "gstAmountExact": applied["totalExact"], "sellingIncludingGstExact": selling_exact + applied["totalExact"]},
        }
    )


def explain_pricing(result: Any) -> str:
    """Developer text view of a pricing result."""
    if not truthy(result):
        return ""
    if result.get("status") == PRICING_STATUS["REJECTED"]:
        return f"REJECTED {result['error']}: {' | '.join(e['message'] for e in result['errors'])}"
    p = result
    lines = [
        f"Total actual project cost  {js_str(jsget(p, 'totalActualProjectCost'))}",
        f"Tier / target / minimum    {js_str(nullish(p['tier'], '—'))} / {js_str(p['targetMarginPct'])}% / {js_str(nullish(p['minimumMarginPct'], '—'))}%",
        f"List price (pre-GST)       {js_str(p['listSellingPriceBeforeGST'])}   = {js_str(jsget(p, 'totalActualProjectCost'))} ÷ (1 − {js_str(p['targetGrossMargin'])})",
    ]
    if truthy(p["discountAmount"]):
        lines.append(f"Discount                   −{js_str(p['discountAmount'])}")
    lines += [
        f"Selling price (pre-GST)    {js_str(p['sellingPriceBeforeGST'])}",
        f"Gross profit               {js_str(p['grossProfit'])}   (actual margin {js_str(p['actualGrossMarginPct'])}%)",
        f"Approval                   {p['approval']}{' — ' + p['approvalReason'] if truthy(p['approvalReason']) else ''}",
        f"GST ({p['gstRegime']}) @ {js_str(p['gstRatePct'])}%   {js_str(p['gstAmount'])}",
    ]
    for c in p["gstComponents"]:
        lines.append(f"  {c['label'].ljust(8)} {js_str(c['valuationPct']).rjust(3)}% of base @ {js_str(c['ratePct'])}%   {js_str(c['taxableValue'])} → {js_str(c['taxAmount'])}")
    lines.append(f"Price incl. GST            {js_str(p['priceIncludingGST'])}")
    if p["extras"]:
        lines += [""] + [f"Extra {e['extraId'].ljust(16)} {js_str(e['customerPriceIncludingGST'])} incl GST  (includedInBase=false)" for e in p["extras"]]
    lines.append(f"Customer total             {js_str(p['customerTotalIncludingGST'])}")
    return "\n".join(lines)
