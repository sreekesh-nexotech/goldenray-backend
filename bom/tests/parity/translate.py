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


def _panel_filter(params: dict) -> str:
    """version-2's tri-state panel filter, passed straight through.

    The platform now takes `panel_type_filter` itself, so the case no longer has to be
    squeezed into the two-state `module_dcr` -- which could not express "either content
    unless a subsidy forces DCR" and so made every `all` case compare against a
    DCR-restricted platform BOM.
    """
    return params.get("panelTypeFilter") or ("DCR" if params.get("moduleDcr", "dcr") == "dcr" else "NON_DCR")


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
        "panel_type_filter": _panel_filter(params),
        # The grade a customer is quoted at, overriding the package profile's default.
        "struct_grade": params.get("structGrade", "default"),
        "micro_brand": params.get("microBrand", "enphase"),
        "zero_export": params.get("zeroExport", True),
        # Subsidy reaches the BOM in version-2: `dcrFilter` forces DCR panels whenever a
        # subsidy is claimed, whatever the filter says (App.jsx:1791). So unlike the
        # `main` corpus it cannot be pinned off here -- doing so would compare a
        # DCR-restricted source BOM against an unrestricted platform one.
        "subsidy_type": params.get("subsidyType", "none"),
        # `roofType` is deliberately not sent: version-2 made it a BOM input (installation
        # cost reads `installationMatrix[size][roofType]`) but the platform has no such
        # field yet -- that lands in Phase C. Sending an unknown key would be rejected by
        # the request serializer, so roof-varying cases differ until then.
    }
    # The source names each cable add-on after the category it draws from.
    extra_cable = [
        {"category": category, "qty": (add_ons.get(key) or {}).get("qty", 0), "component": ""}
        for key, category in (("extraDcCable", "dc_cable"), ("extraAcCable", "ac_cable"), ("armouredCable", "armoured_cable"))
        if (add_ons.get(key) or {}).get("enabled")
    ]
    if extra_cable:
        body["extra_cable"] = extra_cable
    if params.get("futureUpgrade") and params.get("upgradeSize"):
        body["future_size"] = params["upgradeSize"]
        # The source names these includeStructure/includeDB/includeCables/includeMeter.
        opts = params.get("fuOpts") or {}
        body["future_opts"] = {
            "structure": opts.get("includeStructure", True),
            "db": opts.get("includeDB", True),
            "cables": opts.get("includeCables", True),
            "meter": opts.get("includeMeter", True),
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
        "subsidy_type": params.get("subsidyType", "none"),
        # Same substitution as the standard path: the corpus no longer carries
        # `moduleDcr`, so reading it would silently default every case to "dcr".
        "panel_type_filter": _panel_filter(params),
        "upgrade_phase": params.get("upgradePhase", "1P"),
        # The source keys the installation matrix on the standard configurator's `size`,
        # not on the upgrade (App.jsx:2972). Replicated as an upstream bug, so the case's
        # own `size` has to reach the engine -- `size` above carries the target, which the
        # slot sizing needs.
        "base_size_key": params["size"],
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
        "panel_type_filter": _panel_filter(params),
    }
    if params.get("selectedOffer"):
        body["selected_offer_id"] = params["selectedOffer"]
    if sys_type == "upgrade":
        # The discount cases carry no upgrade from/to -- `computeDiscountCalc` reuses
        # whatever `upgradeBom` already held, and the capture pinned the defaults.
        body["upgrade_from_kw"] = 3.0
        body["upgrade_to_kw"] = 5.0
        # `computeDiscountCalc` never builds a BOM: the capture passes it ONE
        # precomputed `upgradeBom` (capture_source.mjs:492,
        # `E.computeUpgradeBom(UPGRADE_DEFAULTS)`), reused for every discount-upgrade
        # case. So its `basePrice` comes from UPGRADE_DEFAULTS, not from the case's own
        # params -- which is why all three tiers report the same ₹170,883.
        #
        # Hence the fixture's values, not the case's: `size: "3"` keys the installation
        # matrix (the upstream quirk, which the capture script itself documents),
        # `upgradeTier: "value"`, `panelTypeFilter: "all"`. Reading the case's `size`
        # ("5") would look up a key the matrix does not hold and price installation per
        # kW against a fixture that paid ₹15,000.
        body["base_size_key"] = "3"
        body["tier"] = "value"
        body["panel_type_filter"] = "all"
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
