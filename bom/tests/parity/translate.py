"""Map a captured Golden-Ray-BOM case onto a platform quote request and response.

The source engines take React state; the platform takes the legacy
``/bom/api/calculate/`` request body. This module is the only place that knows how one
becomes the other, so a naming change lands in one function rather than in every test.

It translates, it does not compute. Anything that needs arithmetic to line up belongs in
``bom/services/website_quote.py``, where the port is held to the corpus — doing it here
would quietly move the target.
"""

from __future__ import annotations

#: Source `addOns.structure.type` → the platform's `structure_type`. The source models
#: "no structure add-on" as flat roof (its templates price everything as a delta over
#: flatRoof), which is what the platform's default means too.
STRUCTURE_TYPES = {"elevated": "elevated", "sheetRoof": "sheetRoof"}


def quote_request(params: dict) -> dict:
    """The platform request body for a captured ``computeBom`` case."""
    add_ons = params.get("addOns") or {}
    structure = add_ons.get("structure") or {}
    body = {
        "sys_type": params["sysType"],
        "size": params["size"],
        "tier": params["tier"],
        "bat_config": params.get("batConfig", "0"),
        "dist_km": params.get("distKm", 100),
        "structure_type": (
            STRUCTURE_TYPES.get(structure.get("type"), "flatRoof") if structure.get("enabled") else "flatRoof"
        ),
        "margin_type": params.get("marginType", "percent"),
        "margin_val": params.get("marginVal", 20),
        "module_dcr": params.get("moduleDcr", "dcr"),
        # The source's BOM memo carries no subsidy or offer; those live in discountCalc,
        # which has its own corpus. Pin them off so a standard case compares only the
        # material/cost/margin ladder.
        "subsidy_type": "none",
    }
    return body


def upgrade_quote_request(params: dict) -> dict:
    """The platform request body for a captured ``computeUpgradeBom`` case."""
    to_kw = params["upgradeToKw"]
    sections = params.get("upgradeSections") or {}
    add_ons = params.get("upgradeAddOns") or {}
    structure = add_ons.get("structure") or {}
    return {
        "sys_type": "upgrade",
        "size": str(to_kw),
        "tier": params.get("upgradeTier", "value"),
        "bat_config": "0",
        "dist_km": params.get("distKm", 100),
        "structure_type": (
            STRUCTURE_TYPES.get(structure.get("type"), "flatRoof") if structure.get("enabled") else "flatRoof"
        ),
        "margin_type": params.get("marginType", "percent"),
        "margin_val": params.get("marginVal", 20),
        "subsidy_type": "none",
        "module_dcr": params.get("moduleDcr", "dcr"),
        "upgrade_phase": params.get("upgradePhase", "1P"),
        "upgrade_from_kw": float(params["upgradeFromKw"]),
        "upgrade_to_kw": float(to_kw),
        # The platform spells the hybrid-inverter section `hybrid_inv`.
        "upgrade_sections": {
            "panels": sections.get("panels", True),
            "structure": sections.get("structure", True),
            "inverter": sections.get("inverter", True),
            "hybrid_inv": sections.get("hybridInv", False),
            "battery": sections.get("battery", False),
            "wiring": sections.get("wiring", True),
        },
    }


def discount_request(params: dict) -> dict:
    """The platform request body for a captured ``computeDiscountCalc`` case."""
    sys_type = params["sysType"]
    body = {
        "sys_type": sys_type,
        "size": params["size"],
        "tier": params.get("upgradeTier" if sys_type == "upgrade" else "tier", "value"),
        "bat_config": "0",
        "subsidy_type": params.get("subsidyType", "none"),
        "ghs_houses": params.get("ghsHouses", 1),
        "module_dcr": params.get("moduleDcr", "dcr"),
    }
    if params.get("selectedOffer"):
        body["selected_offer_id"] = params["selectedOffer"]
    if sys_type == "upgrade":
        body["upgrade_from_kw"] = 3.0
        body["upgrade_to_kw"] = 5.0
    return body


# ── comparable projections ────────────────────────────────────────────────────────────
#
# The source returns React state shaped for its own UI; the platform returns the legacy
# response contract. Only the overlap is meaningful, so each projection below names
# exactly the fields being asserted and nothing else. A field absent from a projection
# is NOT being tested — add it here deliberately, with a reason, rather than widening a
# projection to make a run go green.


def source_totals(result: dict) -> dict:
    """The money ladder of a source ``computeBom``/``computeUpgradeBom`` result.

    ``pre_gst_subtotal`` is deliberately absent. The standard path returns it as
    ``preGstSub``; the upgrade path computes the same quantity inline and never exposes it,
    so comparing the field would assert "the source reports nothing here" rather than
    anything about the arithmetic. ``grand_total`` already pins the whole ladder, since a
    wrong pre-GST cost cannot produce a right grand total.
    """
    totals = result.get("totals") or {}
    return {
        "material_ex_gst": totals.get("matTotal"),
        "gst_total": totals.get("totalGst"),
        "material_with_gst": totals.get("matWithGst"),
        "gst_5": totals.get("gst5"),
        "gst_12": totals.get("gst12"),
        "gst_18": totals.get("gst18"),
        "margin": result.get("mAmt"),
        "grand_total": result.get("grand"),
    }


def platform_totals(body: dict) -> dict:
    """The same ladder out of a platform quote response."""
    totals = body.get("totals") or {}
    return {
        "material_ex_gst": totals.get("material_ex_gst"),
        "gst_total": totals.get("gst_total"),
        "material_with_gst": totals.get("material_with_gst"),
        "gst_5": totals.get("gst_5"),
        "gst_12": totals.get("gst_12"),
        "gst_18": totals.get("gst_18"),
        "margin": totals.get("margin"),
        "grand_total": totals.get("grand_total"),
    }


def source_pricing(result: dict) -> dict:
    return {
        "market_rate": result.get("marketRate"),
        "customer_price": result.get("customerPrice"),
    }


def platform_pricing(body: dict) -> dict:
    pricing = body.get("pricing") or {}
    return {
        "market_rate": pricing.get("market_rate"),
        "customer_price": pricing.get("customer_price"),
    }


def source_lines(result: dict) -> list[dict]:
    """Source BOM lines, reduced to what both sides model.

    Keyed on name rather than position: the source's `pos` is a UI ordering that
    interleaves slots, fixed items, accessories, add-ons and structure, and the platform
    assigns its own. Comparing positions would assert the UI's layout, not the BOM.
    """
    out = []
    for line in result.get("lines") or []:
        out.append(
            {
                "name": line.get("name"),
                "qty": line.get("qty"),
                "unit_price": line.get("unitPrice"),
                "amount": line.get("amount"),
                "gst_pct": line.get("gst"),
            }
        )
    return sorted(out, key=lambda row: (str(row["name"]), row["qty"] or 0))


def platform_lines(body: dict) -> list[dict]:
    out = []
    for line in body.get("bom_lines") or []:
        out.append(
            {
                "name": line.get("name"),
                "qty": line.get("qty"),
                "unit_price": line.get("unit_price"),
                "amount": line.get("amount"),
                "gst_pct": line.get("gst_pct"),
            }
        )
    return sorted(out, key=lambda row: (str(row["name"]), row["qty"] or 0))


def source_subsidy(result: dict) -> dict:
    return {
        "discount_amt": result.get("discountAmt"),
        "final_price": result.get("finalPrice"),
        "subsidy_amt": result.get("subsidy"),
        "price_after_subsidy": result.get("priceAfterSubsidy"),
    }


def platform_subsidy(body: dict) -> dict:
    pricing = body.get("pricing") or {}
    return {
        "discount_amt": pricing.get("discount_amt"),
        "final_price": pricing.get("final_price"),
        "subsidy_amt": pricing.get("subsidy_amt"),
        "price_after_subsidy": pricing.get("price_after_subsidy"),
    }
