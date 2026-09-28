"""Actual project cost and landed cost — ports of Flarize ``costEngine.js`` (``costEngine.3``),
``procurementPriceMaster.js`` and ``procurementBatch.js``.

:func:`calculate_cost` computes the ACTUAL PROJECT COST of a LOCKED BOM snapshot in ten heads (material at LANDED cost,
structure, installation, site survey, engineering/design, transportation, service/AMC, special works, office
allocation, miscellaneous % of direct cost). There is no silent fallback: every gap is an explicit error code and
makes the total ``None``. Rates come from the Project Head rate card (:mod:`engines.rate_card`) first, then the plain
cost configuration — never a nearest size or a first-available rate.

:func:`allocate_landed` builds landed unit costs for a procurement batch by PURCHASE-VALUE PROPORTION (the only
approved method), reconciled by largest remainder so the allocated amounts sum exactly to the charges.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from engines._jscompat import (
    clean,
    is_array,
    is_nullish,
    js_floor,
    js_number,
    js_or,
    js_round,
    js_str,
    js_str_key,
    jsget,
    nullish,
    prop_key,
    truthy,
)
from engines._money_compat import is_money, mul_exact, round_money, sum_exact

# PLAN §2.3: "LIST is never derived by markup in code … gross-margin check lives in engines.cost". The one list-price
# formula (cost / (1 − m); MARKUP raises) is defined next to the pricing engine and exposed here under that name.
from engines.pricing import PricingError, gross_margin_list_price, validate_gross_margin  # noqa: F401
from engines.rate_card import RATE_TYPE, installation_key, resolve_rate

COST_ENGINE_VERSION = "costEngine.3"

COST_HEAD = {
    name: name
    for name in (
        "MATERIAL_LANDED",
        "STRUCTURE",
        "INSTALLATION",
        "SITE_SURVEY",
        "ENGINEERING_DESIGN",
        "TRANSPORTATION",
        "SERVICE_AMC",
        "SPECIAL_PROJECT_WORKS",
        "OFFICE_EXPENSE_ALLOCATION",
        "MISCELLANEOUS",
    )
}
DIRECT_COST_HEADS = (
    "MATERIAL_LANDED",
    "STRUCTURE",
    "INSTALLATION",
    "SITE_SURVEY",
    "ENGINEERING_DESIGN",
    "TRANSPORTATION",
    "SERVICE_AMC",
    "SPECIAL_PROJECT_WORKS",
    "OFFICE_EXPENSE_ALLOCATION",
)
COST_HEAD_ORDER = DIRECT_COST_HEADS + ("MISCELLANEOUS",)
COST_HEAD_OWNER = {
    "MATERIAL_LANDED": "PROCUREMENT",
    "STRUCTURE": "PROJECT_HEAD",
    "INSTALLATION": "PROJECT_HEAD",
    "SITE_SURVEY": "PROJECT_HEAD",
    "ENGINEERING_DESIGN": "PROJECT_HEAD",
    "TRANSPORTATION": "PROJECT_HEAD",
    "SERVICE_AMC": "PROJECT_HEAD",
    "SPECIAL_PROJECT_WORKS": "PROJECT_HEAD",
    "OFFICE_EXPENSE_ALLOCATION": "ADMIN",
    "MISCELLANEOUS": "ADMIN",
}
COST_ERROR = {
    name: name
    for name in (
        "COST_NOT_AVAILABLE_UNTIL_BOM_LOCKED",
        "LANDED_COST_NOT_CONFIGURED",
        "PRICE_KIND_INVALID",
        "STRUCTURE_RATE_NOT_CONFIGURED",
        "INSTALLATION_COST_NOT_CONFIGURED",
        "SITE_SURVEY_NOT_CONFIGURED",
        "SITE_SURVEY_EXCESS_RATE_NOT_CONFIGURED",
        "ENGINEERING_DESIGN_NOT_CONFIGURED",
        "TRANSPORT_RATE_NOT_CONFIGURED",
        "TRANSPORT_DATA_INCOMPLETE",
        "SERVICE_CONFIG_INCOMPLETE",
        "SERVICE_OUT_OF_COVERAGE",
        "SPECIAL_WORK_INVALID",
        "OFFICE_ALLOCATION_INVALID",
        "MISC_CONFIG_INVALID",
    )
}
COST_STATUS = {"COMPLETE": "COMPLETE", "INCOMPLETE": "INCOMPLETE", "UNAVAILABLE": "UNAVAILABLE"}
SNAPSHOT_STATUS = {"DRAFT": "DRAFT", "LOCKED": "LOCKED", "SUPERSEDED": "SUPERSEDED"}
SPECIAL_WORK_TYPE = {
    name: name
    for name in (
        "CIVIL_WORK",
        "TRENCHING",
        "SCAFFOLDING",
        "CRANE",
        "ROOF_REINFORCEMENT",
        "DISMANTLING",
        "WATERPROOFING",
        "CABLE_ROUTE",
        "DIFFICULT_ACCESS",
        "PANEL_MODIFICATION",
        "OTHER",
    )
}

# procurementPriceMaster.js
PRICE_KIND = {"PURCHASE_PRICE": "PURCHASE_PRICE", "LANDED_UNIT_COST": "LANDED_UNIT_COST", "REFERENCE_PRICE": "REFERENCE_PRICE", "UNKNOWN": "UNKNOWN"}
VALID_FOR_PROJECT_COST = ("LANDED_UNIT_COST",)
FORBIDDEN_PRICE_KINDS = ("SELLING_PRICE", "CUSTOMER_PRICE", "MARKET_RATE")
PRICE_RECORD_FIELDS = (
    "componentId",
    "componentName",
    "purchasePrice",
    "purchasePriceVersion",
    "purchasePriceEffectiveFrom",
    "landedUnitCost",
    "landedCostVersion",
    "landedCostEffectiveFrom",
    "supplier",
    "supplierReference",
    "currency",
    "notes",
)

# procurementBatch.js
ALLOCATION_METHOD = {"PURCHASE_VALUE_PROPORTION": "PURCHASE_VALUE_PROPORTION"}
FORBIDDEN_ALLOCATION_METHODS = ("WEIGHT", "VOLUME", "QUANTITY", "EQUAL_SPLIT")
BATCH_ERROR = {
    "ALLOCATION_METHOD_INVALID": "ALLOCATION_METHOD_INVALID",
    "BATCH_LINE_INVALID": "BATCH_LINE_INVALID",
    "ZERO_PURCHASE_VALUE": "ZERO_PURCHASE_VALUE",
    "PROJECT_TRANSPORT_NOT_ALLOWED": "PROJECT_TRANSPORT_NOT_ALLOWED",
    "BATCH_CHARGE_INVALID": "BATCH_CHARGE_INVALID",
}
# PLAN §2.4 procurement_batch_charge.kind — every one is a supplier → warehouse charge.
BATCH_CHARGE_KINDS = ("FREIGHT", "INSURANCE", "HANDLING", "DUTY", "OTHER")


def _camel(name: str) -> str:
    parts = name.lower().split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


# ---------------------------------------------------------------------------------------------------------------
# Procurement price master
# ---------------------------------------------------------------------------------------------------------------


def to_price_record(raw: Any = None) -> dict:
    """Normalise a procurement record: nothing derived, absent facts ``None``; a selling-price kind is a violation."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {field: raw.get(field) for field in PRICE_RECORD_FIELDS}
    out["currency"] = js_or(jsget(raw, "currency"), "INR")
    for kind in FORBIDDEN_PRICE_KINDS:
        if kind in raw or _camel(kind) in raw:
            out["violation"] = f"A component-level {kind} is not part of the V1 model (§18). " "Procurement owns purchasePrice and landedUnitCost; project selling price belongs to the Pricing Engine."
    out["hasPurchasePrice"] = is_money(out["purchasePrice"])
    out["hasLandedCost"] = is_money(out["landedUnitCost"])
    out["landedCostSource"] = "PROCUREMENT_ENTERED" if out["hasLandedCost"] else None
    out["missing"] = [field for field in PRICE_RECORD_FIELDS if out[field] is None]
    return out


def build_price_master(raw: Any = None) -> dict:
    """``{componentId: price record}`` from a raw price master keyed by component id."""
    return {cid: to_price_record({"componentId": cid, **(record if isinstance(record, dict) else {})}) for cid, record in (raw or {}).items()}


def project_unit_cost(record: Any) -> dict:
    """What the cost engine may use for one component: the LANDED unit cost only (``{ok, unitCost, kind, reason}``)."""
    if not truthy(record):
        return {"ok": False, "unitCost": None, "kind": PRICE_KIND["UNKNOWN"], "reason": "No procurement price record for this component."}
    if truthy(jsget(record, "violation")):
        return {"ok": False, "unitCost": None, "kind": PRICE_KIND["UNKNOWN"], "reason": record["violation"]}
    if not truthy(jsget(record, "hasLandedCost")):
        reason = (
            "landedUnitCost is not recorded. The purchase price must NOT be substituted for it — procurement-side logistics, "
            "handling and warehousing are not included in the supplier price and are never derived by the system."
            if truthy(jsget(record, "hasPurchasePrice"))
            else "Neither landedUnitCost nor purchasePrice is recorded."
        )
        return {"ok": False, "unitCost": None, "kind": PRICE_KIND["UNKNOWN"], "reason": reason}
    return {"ok": True, "unitCost": js_number(record["landedUnitCost"]), "kind": PRICE_KIND["LANDED_UNIT_COST"], "reason": None}


def procurement_uplift(record: Any) -> dict | None:
    """Landed minus purchase, and as % of purchase (``None`` without both facts)."""
    if not truthy(jsget(record, "hasLandedCost")) or not truthy(jsget(record, "hasPurchasePrice")):
        return None
    purchase = js_number(record["purchasePrice"])
    landed = js_number(record["landedUnitCost"])
    delta = landed - purchase
    return {"purchasePrice": purchase, "landedUnitCost": landed, "delta": delta, "pctOfPurchase": None if purchase == 0 else (delta / purchase) * 100}


# ---------------------------------------------------------------------------------------------------------------
# Cost engine
# ---------------------------------------------------------------------------------------------------------------


class _Heads:
    def __init__(self):
        self.heads: dict[str, dict] = {}
        self.errors: list[dict] = []

    def fail(self, code: str, head: str | None, message: str, detail: Any = None) -> None:
        self.errors.append({"code": code, "head": head, "message": message, "detail": nullish(detail, None)})

    def add(self, head: str, amount_exact: Any, inputs: dict, formula: str, **extra: Any) -> None:
        entry = {
            "head": head,
            "owner": COST_HEAD_OWNER[head],
            "inputs": clean(inputs),
            "formula": formula,
            "amount": None if amount_exact is None else round_money(amount_exact),
            "amountExact": None if amount_exact is None else amount_exact,
            "source": nullish(extra.get("source"), None),
            "configVersion": nullish(extra.get("version"), None),
            "status": js_or(extra.get("status"), "INCOMPLETE" if amount_exact is None else "OK"),
            "note": js_or(extra.get("note"), None),
        }
        if truthy(extra.get("lines")):
            entry["lines"] = extra["lines"]
        self.heads[head] = entry


def _material(snapshot: dict, price_master: dict, versions: dict, heads: _Heads) -> None:
    lines = [line for line in (snapshot.get("lines") or [])]
    lines.sort(key=lambda line: js_str_key(js_str(js_or(jsget(line, "componentId"), ""))))
    material_lines = []
    for line in lines:
        cid = jsget(line, "componentId")
        master_record = price_master.get(prop_key(cid)) if isinstance(price_master, dict) else None
        record = to_price_record({"componentId": cid, **(master_record if truthy(master_record) and isinstance(master_record, dict) else {})})
        resolved = project_unit_cost(record)
        qty = js_number(nullish(jsget(line, "quantity"), 0))
        extended = mul_exact(qty, resolved["unitCost"]) if resolved["ok"] else None
        if not resolved["ok"]:
            heads.fail(
                COST_ERROR["PRICE_KIND_INVALID"] if truthy(record.get("violation")) else COST_ERROR["LANDED_COST_NOT_CONFIGURED"],
                COST_HEAD["MATERIAL_LANDED"],
                f'"{js_str(cid)}": {resolved["reason"]}',
                clean({"componentId": cid, "role": jsget(line, "role"), "hasPurchasePrice": record["hasPurchasePrice"]}),
            )
        material_lines.append(
            clean(
                {
                    "componentId": cid,
                    "componentName": nullish(record.get("componentName"), jsget(line, "componentName"), None),
                    "role": nullish(jsget(line, "role"), None),
                    "quantity": qty,
                    "purchasePrice": js_number(record["purchasePrice"]) if record["hasPurchasePrice"] else None,
                    "purchasePriceVersion": record["purchasePriceVersion"],
                    "landedUnitCost": resolved["unitCost"] if resolved["ok"] else None,
                    "landedCostVersion": record["landedCostVersion"],
                    "landedCostEffectiveFrom": record["landedCostEffectiveFrom"],
                    "landedCostSource": record["landedCostSource"],
                    "procurementUplift": procurement_uplift(record),
                    "priceKindUsed": PRICE_KIND["LANDED_UNIT_COST"] if resolved["ok"] else PRICE_KIND["UNKNOWN"],
                    "extendedCost": None if extended is None else round_money(extended),
                    "extendedExact": extended,
                    "supplier": record["supplier"],
                    "supplierReference": record["supplierReference"],
                    "componentDataVersion": nullish(jsget(line, "componentDataVersion"), None),
                    "formula": f"{js_str(qty)} × {js_str(resolved['unitCost'])} (landed)" if resolved["ok"] else None,
                    "status": "OK" if resolved["ok"] else COST_ERROR["LANDED_COST_NOT_CONFIGURED"],
                }
            )
        )
    material_ok = all(line["extendedExact"] is not None for line in material_lines)
    heads.add(
        "MATERIAL_LANDED",
        sum_exact(line["extendedExact"] for line in material_lines) if material_ok else None,
        {"lineCount": len(material_lines), "landedCostVersion": versions["landed"], "procurementPriceVersion": versions["procurement"]},
        "SUM(quantity × landedUnitCost)",
        version=versions["landed"],
        source="Procurement price master — landed cost",
        lines=material_lines,
        status="OK" if material_ok else COST_ERROR["LANDED_COST_NOT_CONFIGURED"],
        note=("Landed cost, not purchase price. Supplier→warehouse logistics live inside landedUnitCost and are never re-charged as project transportation."),
    )


def _structure(cfg: dict, project: dict, heads: _Heads) -> Any:
    structure = js_or(jsget(cfg, "structure"), {})
    inst_type = js_or(jsget(project, "installationType"), jsget(project, "roofType"), None)
    add_ons = js_or(jsget(structure, "roofAddOn"), [])
    add_on = next((r for r in add_ons if jsget(r, "installationType") == inst_type or jsget(r, "roofType") == inst_type), None)
    if not truthy(inst_type) or add_on is None:
        heads.fail(
            COST_ERROR["STRUCTURE_RATE_NOT_CONFIGURED"],
            "STRUCTURE",
            f'No approved structure rate for installationType "{js_str(nullish(inst_type, "undeclared"))}". Not assumed to be zero.',
            {"instType": inst_type},
        )
        heads.add(
            "STRUCTURE",
            None,
            {"installationType": inst_type},
            "roofAddOn + labour + repairAllowance",
            version=jsget(structure, "version"),
            source=jsget(structure, "source"),
            status=COST_ERROR["STRUCTURE_RATE_NOT_CONFIGURED"],
        )
        return inst_type
    labour = js_number(nullish(jsget(jsget(structure, "labour"), "rate"), 0))
    repair_pct = js_number(nullish(jsget(jsget(structure, "repairAllowancePct"), "value"), 0))
    material_cost = js_number(nullish(jsget(project, "structureMaterialCost"), 0))
    heads.add(
        "STRUCTURE",
        sum_exact([jsget(add_on, "rate"), labour, mul_exact(material_cost, repair_pct / 100)]),
        {
            "installationType": inst_type,
            "addOnRate": jsget(add_on, "rate"),
            "unit": jsget(add_on, "unit"),
            "labourRate": labour,
            "repairAllowancePct": repair_pct,
            "structureMaterialBase": material_cost,
        },
        f"{js_str(jsget(add_on, 'rate'))} + {js_str(labour)} + ({js_str(material_cost)} × {js_str(repair_pct)}%)",
        version=jsget(structure, "version"),
        source=jsget(structure, "source"),
        note="Flat-roof structure MATERIAL is emitted as BOM lines and is counted under MATERIAL_LANDED.",
    )
    return inst_type


def _installation(cfg: dict, size_kw: Any, inst_type: Any, from_card, heads: _Heads) -> None:
    installation = js_or(jsget(cfg, "installation"), {})
    card = from_card(RATE_TYPE["INSTALLATION"], installation_key(size_kw, inst_type))
    if card["ok"]:
        rate = card["value"]
        version = jsget(card, "versionId")
    else:
        rates = js_or(jsget(installation, "rates"), [])
        rate = next((r for r in rates if js_number(jsget(r, "systemSizeKw")) == size_kw and jsget(r, "installationType") == inst_type), None)
        version = nullish(jsget(rate, "version"), jsget(installation, "version"))
    type_label = js_str(nullish(inst_type, "undeclared"))
    if not truthy(rate):
        heads.fail(
            COST_ERROR["INSTALLATION_COST_NOT_CONFIGURED"],
            "INSTALLATION",
            f"No installation rate configured for {js_str(size_kw)} kW / {type_label}. " "The nearest system size is NEVER used. Configure the exact combination on the Project Head rate card.",
            {"sizeKw": size_kw, "installationType": inst_type},
        )
        heads.add(
            "INSTALLATION",
            None,
            {"systemSizeKw": size_kw, "installationType": inst_type},
            "PER_PROJECT: rate | PER_KW: rate x sizeKw",
            version=version,
            status=COST_ERROR["INSTALLATION_COST_NOT_CONFIGURED"],
        )
        return
    unit = jsget(rate, "unit")
    if unit not in ("PER_KW", "PER_PROJECT"):
        heads.fail(
            COST_ERROR["INSTALLATION_COST_NOT_CONFIGURED"],
            "INSTALLATION",
            f"Installation rate for {js_str(size_kw)} kW / {js_str(inst_type)} does not declare a valid unit. "
            "PER_KW or PER_PROJECT must be stated explicitly on the rate record; the engine never assumes one.",
        )
        heads.add(
            "INSTALLATION",
            None,
            {"systemSizeKw": size_kw, "installationType": inst_type, "unit": nullish(unit, None)},
            "rate x unit",
            version=version,
            status=COST_ERROR["INSTALLATION_COST_NOT_CONFIGURED"],
        )
        return
    amount = mul_exact(jsget(rate, "rate"), size_kw) if unit == "PER_KW" else js_number(jsget(rate, "rate"))
    heads.add(
        "INSTALLATION",
        amount,
        {
            "systemSizeKw": size_kw,
            "installationType": inst_type,
            "unit": unit,
            "rate": jsget(rate, "rate"),
            "effectiveFrom": jsget(card, "effectiveFrom") if card["ok"] else nullish(jsget(rate, "effectiveFrom"), None),
        },
        f"{js_str(jsget(rate, 'rate'))} × {js_str(size_kw)} kW" if unit == "PER_KW" else f"{js_str(jsget(rate, 'rate'))} (per project)",
        version=version,
        source="PROJECT_HEAD_RATE_CARD" if card["ok"] else jsget(installation, "source"),
    )


def _site_survey(cfg: dict, project: dict, from_card, heads: _Heads) -> None:
    card = from_card(RATE_TYPE["SITE_SURVEY"], "STANDARD")
    base = js_or(jsget(cfg, "siteSurvey"), {})
    survey = {**base, **card["value"]} if card["ok"] else base
    version = jsget(card, "versionId") if card["ok"] else jsget(survey, "version")
    source = "PROJECT_HEAD_RATE_CARD" if card["ok"] else jsget(survey, "source")
    dist = jsget(project, "distanceKm")
    cost = jsget(survey, "costPerProject")
    coverage = jsget(survey, "coverageKm")
    excess = jsget(survey, "excessRatePerKm")
    if jsget(project, "siteSurveyIncluded") is False:
        heads.add("SITE_SURVEY", 0, {"siteSurveyIncluded": False}, "0 (survey explicitly not included in this project)", version=version)
    elif not is_money(cost) or not is_money(coverage):
        heads.fail(COST_ERROR["SITE_SURVEY_NOT_CONFIGURED"], "SITE_SURVEY", "Site survey cost or coverage distance is not configured. Not assumed to be zero.")
        heads.add("SITE_SURVEY", None, {}, "costPerProject (+ excess beyond coverageKm)", version=version, status=COST_ERROR["SITE_SURVEY_NOT_CONFIGURED"])
    elif not is_money(dist):
        heads.fail(COST_ERROR["SITE_SURVEY_NOT_CONFIGURED"], "SITE_SURVEY", "distanceKm is not recorded, so site-survey coverage cannot be determined.")
        heads.add(
            "SITE_SURVEY",
            None,
            {"distanceKm": None, "coverageKm": js_number(coverage)},
            "costPerProject (+ excess beyond coverageKm)",
            version=version,
            status=COST_ERROR["SITE_SURVEY_NOT_CONFIGURED"],
        )
    elif js_number(dist) <= js_number(coverage):
        heads.add(
            "SITE_SURVEY",
            js_number(cost),
            {"costPerProject": js_number(cost), "distanceKm": js_number(dist), "coverageKm": js_number(coverage), "excessKm": 0, "unit": "PER_PROJECT"},
            f"{js_str(cost)} (per project, within {js_str(coverage)} km)",
            version=version,
            source=source,
            note=("ALL-INCLUSIVE: engineer cost, engineer travel, site visit and survey report. " "No separate engineer travel is added; project transportation is a different head."),
        )
    elif not is_money(excess):
        heads.fail(
            COST_ERROR["SITE_SURVEY_EXCESS_RATE_NOT_CONFIGURED"],
            "SITE_SURVEY",
            f"Distance {js_str(dist)} km exceeds the {js_str(coverage)} km coverage but no excess per-km rate is configured. " "No rate is invented and the flat charge is not applied on its own.",
            {"distanceKm": js_number(dist), "coverageKm": js_number(coverage)},
        )
        heads.add(
            "SITE_SURVEY",
            None,
            {"distanceKm": js_number(dist), "coverageKm": js_number(coverage), "excessRatePerKm": None},
            "costPerProject + (distanceKm - coverageKm) x excessRatePerKm",
            version=version,
            status=COST_ERROR["SITE_SURVEY_EXCESS_RATE_NOT_CONFIGURED"],
        )
    else:
        excess_km = js_number(dist) - js_number(coverage)
        heads.add(
            "SITE_SURVEY",
            sum_exact([js_number(cost), mul_exact(excess_km, excess)]),
            {
                "costPerProject": js_number(cost),
                "distanceKm": js_number(dist),
                "coverageKm": js_number(coverage),
                "excessKm": excess_km,
                "excessRatePerKm": js_number(excess),
                "unit": "PER_PROJECT_PLUS_EXCESS_KM",
            },
            f"{js_str(cost)} + ({js_str(dist)} - {js_str(coverage)}) × {js_str(excess)}",
            version=version,
            source=source,
            note=("The coverage threshold is an approved business rule and is never silently changed. " "The excess per-km rate is Project Head configuration on the rate card."),
        )


def _engineering_design(cfg: dict, size_kw: Any, from_card, heads: _Heads) -> None:
    design = js_or(jsget(cfg, "engineeringDesign"), {})
    limit = js_number(nullish(jsget(design, "sizeLimitKw"), math.nan))
    finite = math.isfinite(limit)
    within = finite and size_kw <= limit
    band = (f"<={js_str(limit)}kW" if within else f">{js_str(limit)}kW") if finite else "UNBANDED"
    card = from_card(RATE_TYPE["ENGINEERING_DESIGN"], band)
    if card["ok"]:
        rate = card["value"]
    elif within and is_money(jsget(design, "costPerProject")):
        rate = {"costPerProject": design["costPerProject"], "unit": "PER_PROJECT", "sizeLimitKw": limit}
    else:
        rate = None
    version = jsget(card, "versionId") if card["ok"] else jsget(design, "version")
    if not finite or not truthy(rate) or not is_money(jsget(rate, "costPerProject")):
        message = (
            f"System size {js_str(size_kw)} kW is above the {js_str(limit)} kW band and the Project Head rate card has no " f'"{band}" rate. The flat rate is NOT extrapolated and no rate is invented.'
            if finite and not within
            else "Engineering/design rate or size band is not configured. Not silently zero, never folded into installation."
        )
        heads.fail(COST_ERROR["ENGINEERING_DESIGN_NOT_CONFIGURED"], "ENGINEERING_DESIGN", message, {"sizeKw": size_kw, "sizeLimitKw": limit if finite else None, "band": band})
        heads.add(
            "ENGINEERING_DESIGN",
            None,
            {"sizeKw": size_kw, "band": band, "sizeLimitKw": limit if finite else None},
            "rate card rate for the applicable size band",
            version=version,
            status=COST_ERROR["ENGINEERING_DESIGN_NOT_CONFIGURED"],
        )
    elif jsget(rate, "unit") == "PER_KW":
        heads.add(
            "ENGINEERING_DESIGN",
            mul_exact(rate["costPerProject"], size_kw),
            {"sizeKw": size_kw, "band": band, "rate": rate["costPerProject"], "unit": "PER_KW"},
            f"{js_str(rate['costPerProject'])} × {js_str(size_kw)} kW",
            version=version,
            source="PROJECT_HEAD_RATE_CARD",
            note="PER_KW is used only because the rate record for this band explicitly declares it.",
        )
    else:
        heads.add(
            "ENGINEERING_DESIGN",
            js_number(rate["costPerProject"]),
            {"sizeKw": size_kw, "band": band, "costPerProject": js_number(rate["costPerProject"]), "unit": "PER_PROJECT", "sizeLimitKw": limit},
            f"{js_str(rate['costPerProject'])} (per project, band {band})",
            version=version,
            source="PROJECT_HEAD_RATE_CARD" if card["ok"] else jsget(design, "source"),
            note=(
                "ALL-INCLUSIVE: engineer cost, engineer travel, engineering/design work and the site report. "
                "Engineer travel is never added separately and the cost is never computed per kW within the band."
            ),
        )


def _transportation(cfg: dict, project: dict, from_card, heads: _Heads) -> None:
    transport = js_or(jsget(cfg, "transportation"), {})
    vehicle_type = js_or(jsget(project, "vehicleType"), None)
    card = from_card(RATE_TYPE["TRANSPORT_VEHICLE"], vehicle_type) if truthy(vehicle_type) else {"ok": False}
    vehicles = js_or(jsget(transport, "vehicles"), [])
    vehicle = card["value"] if card["ok"] else next((v for v in vehicles if jsget(v, "vehicleType") == vehicle_type), None)
    dist = jsget(project, "distanceKm")
    if jsget(transport, "notApplicable") is True:
        heads.add("TRANSPORTATION", 0, {"notApplicable": True}, "0 (project transportation explicitly not applicable)", version=jsget(transport, "version"))
    elif not truthy(vehicle_type) or not truthy(vehicle):
        heads.fail(
            COST_ERROR["TRANSPORT_RATE_NOT_CONFIGURED"],
            "TRANSPORTATION",
            f'No configured vehicle rate for vehicleType "{js_str(nullish(vehicle_type, "undeclared"))}". ' "No universal per-km rate is assumed. Project Head must configure vehicle types and rates.",
            {"vehicleType": vehicle_type, "configured": [nullish(jsget(v, "vehicleType"), None) for v in vehicles]},
        )
        heads.add(
            "TRANSPORTATION",
            None,
            {"vehicleType": vehicle_type, "distanceKm": js_number(dist) if is_money(dist) else None},
            "distanceKm × vehicle.ratePerKm",
            version=jsget(transport, "version"),
            status=COST_ERROR["TRANSPORT_RATE_NOT_CONFIGURED"],
        )
    elif not is_money(dist):
        heads.fail(
            COST_ERROR["TRANSPORT_DATA_INCOMPLETE"],
            "TRANSPORTATION",
            "distanceKm is not recorded on the project. Distance is a project input and is never derived automatically in V1.",
        )
        heads.add(
            "TRANSPORTATION",
            None,
            {"vehicleType": vehicle_type, "ratePerKm": jsget(vehicle, "ratePerKm"), "distanceKm": None},
            "distanceKm × vehicle.ratePerKm",
            version=jsget(transport, "version"),
            status=COST_ERROR["TRANSPORT_DATA_INCOMPLETE"],
        )
    else:
        heads.add(
            "TRANSPORTATION",
            mul_exact(dist, jsget(vehicle, "ratePerKm")),
            {
                "vehicleType": nullish(jsget(vehicle, "vehicleType"), vehicle_type),
                "ratePerKm": js_number(jsget(vehicle, "ratePerKm")),
                "distanceKm": js_number(dist),
                "distanceBasis": "ONE_WAY",
            },
            f"{js_str(dist)} × {js_str(jsget(vehicle, 'ratePerKm'))}",
            version=jsget(card, "versionId") if card["ok"] else nullish(jsget(vehicle, "version"), jsget(transport, "version")),
            source="PROJECT_HEAD_RATE_CARD" if card["ok"] else jsget(transport, "source"),
            note=(
                "Warehouse → site delivery only. Supplier → warehouse logistics are inside landedUnitCost and are not charged again here. "
                "Distance is the one-way billable distance and is NEVER multiplied by 2 by the system."
            ),
        )


def _service(cfg: dict, size_kw: Any, heads: _Heads) -> None:
    service = js_or(jsget(cfg, "serviceAmc"), {})
    version = jsget(service, "version")
    if jsget(service, "serviceIncluded") is False:
        heads.add("SERVICE_AMC", 0, {"serviceIncluded": False}, "0 (service explicitly not included)", version=version)
        return
    fields = ("costPerVisit", "visitsPerYear", "serviceYears")
    missing = [f for f in fields if not is_money(jsget(service, f))]
    limit = jsget(service, "sizeLimitKw")
    if missing:
        values = {f: nullish(jsget(service, f), None) for f in fields}
        heads.fail(
            COST_ERROR["SERVICE_CONFIG_INCOMPLETE"],
            "SERVICE_AMC",
            f"Service configuration is incomplete: {', '.join(missing)} not recorded. " "None is invented, no annual-rate fallback exists, and none silently becomes zero.",
            {"missing": missing, **values},
        )
        heads.add(
            "SERVICE_AMC",
            None,
            {**values, "missing": missing},
            "costPerVisit × visitsPerYear × serviceYears",
            version=version,
            status=COST_ERROR["SERVICE_CONFIG_INCOMPLETE"],
        )
    elif is_money(limit) and size_kw > js_number(limit):
        heads.fail(
            COST_ERROR["SERVICE_OUT_OF_COVERAGE"],
            "SERVICE_AMC",
            f"System size {js_str(size_kw)} kW exceeds the {js_str(limit)} kW service model limit. No rate is approved above it.",
            {"sizeKw": size_kw, "sizeLimitKw": js_number(limit)},
        )
        heads.add(
            "SERVICE_AMC",
            None,
            {"sizeKw": size_kw, "sizeLimitKw": js_number(limit)},
            "above size limit — approval required",
            version=version,
            status=COST_ERROR["SERVICE_OUT_OF_COVERAGE"],
        )
    else:
        heads.add(
            "SERVICE_AMC",
            mul_exact(mul_exact(service["costPerVisit"], service["visitsPerYear"]), service["serviceYears"]),
            {
                "costPerVisit": js_number(service["costPerVisit"]),
                "visitsPerYear": js_number(service["visitsPerYear"]),
                "serviceYears": js_number(service["serviceYears"]),
                "sizeLimitKw": nullish(limit, None),
                "sizeKw": size_kw,
            },
            f"{js_str(service['costPerVisit'])} × {js_str(service['visitsPerYear'])} × {js_str(service['serviceYears'])}",
            version=version,
            source=jsget(service, "source"),
            note=("Decision C9. Per-visit model — the visits-per-year term is explicit, never folded into an annual rate. " "This is an INTERNAL cost and is never a customer-facing AMC price."),
        )


def _special_works(project: dict, heads: _Heads) -> None:
    works = jsget(project, "specialWorks")
    works = list(works) if is_array(works) else []
    works.sort(key=lambda w: js_str_key(js_str(js_or(jsget(w, "workType"), ""))))
    lines = []
    for work in works:
        work_type = jsget(work, "workType")
        if not truthy(work_type) or not is_money(jsget(work, "amount")):
            heads.fail(
                COST_ERROR["SPECIAL_WORK_INVALID"],
                "SPECIAL_PROJECT_WORKS",
                f'Special work "{js_str(nullish(work_type, "(no type)"))}" has no valid amount. No rate is invented.',
                work,
            )
            lines.append(
                {
                    "workType": nullish(work_type, None),
                    "amount": None,
                    "extendedCost": None,
                    "reason": nullish(jsget(work, "reason"), None),
                    "addedBy": nullish(jsget(work, "addedBy"), None),
                    "at": nullish(jsget(work, "at"), None),
                    "status": COST_ERROR["SPECIAL_WORK_INVALID"],
                }
            )
            continue
        amount = js_number(work["amount"])
        lines.append(
            {
                "workType": work_type,
                "amount": amount,
                "extendedCost": round_money(amount),
                "extendedExact": amount,
                "reason": nullish(jsget(work, "reason"), None),
                "addedBy": nullish(jsget(work, "addedBy"), None),
                "at": nullish(jsget(work, "at"), None),
                "status": "OK",
            }
        )
    ok = all(line.get("extendedExact") is not None for line in lines)
    heads.add(
        "SPECIAL_PROJECT_WORKS",
        sum_exact(js_or(line.get("extendedExact"), 0) for line in lines) if ok else None,
        {"workCount": len(lines)},
        "SUM(explicitly added special works)" if lines else "0 (no special work added by Project Head)",
        version=None,
        source="Project Head, per project",
        lines=lines,
        status="OK" if ok else COST_ERROR["SPECIAL_WORK_INVALID"],
        note="Never added automatically. Each work is its own visible line and is never hidden inside miscellaneous.",
    )


def _office(cfg: dict, heads: _Heads) -> None:
    office = js_or(jsget(cfg, "officeExpenseAllocation"), {})
    monthly = jsget(office, "monthlyOfficeExpense")
    expected_raw = jsget(office, "expectedProjectsPerMonth")
    expected = js_number(nullish(expected_raw, 0))
    if not is_money(monthly) or not expected > 0:
        values = {"monthlyOfficeExpense": nullish(monthly, None), "expectedProjectsPerMonth": nullish(expected_raw, None)}
        heads.fail(
            COST_ERROR["OFFICE_ALLOCATION_INVALID"],
            "OFFICE_EXPENSE_ALLOCATION",
            "monthlyOfficeExpense must be configured and expectedProjectsPerMonth must be > 0 "
            f"(got {js_str(nullish(monthly, 'null'))} ÷ {js_str(nullish(expected_raw, 'null'))}). "
            "No silent fallback to zero and no division by zero.",
            values,
        )
        heads.add(
            "OFFICE_EXPENSE_ALLOCATION",
            None,
            values,
            "monthlyOfficeExpense ÷ expectedProjectsPerMonth",
            version=jsget(office, "version"),
            status=COST_ERROR["OFFICE_ALLOCATION_INVALID"],
        )
        return
    heads.add(
        "OFFICE_EXPENSE_ALLOCATION",
        js_number(monthly) / expected,
        {"monthlyOfficeExpense": js_number(monthly), "expectedProjectsPerMonth": expected},
        f"{js_str(monthly)} ÷ {js_str(expected)}",
        version=jsget(office, "version"),
        source=jsget(office, "source"),
        note="Allocated on the ADMIN-defined expected volume, never on actual completed projects.",
    )


def _miscellaneous(cfg: dict, direct_exact: Any, heads: _Heads) -> None:
    misc = js_or(jsget(cfg, "miscellaneous"), {})
    pct = jsget(misc, "percentage")
    version = jsget(misc, "version")
    formula = "DIRECT_PROJECT_COST × miscellaneousPct"
    if not is_money(pct):
        heads.fail(
            COST_ERROR["MISC_CONFIG_INVALID"],
            "MISCELLANEOUS",
            "A miscellaneous allowance percentage must be explicitly configured (0% is a valid explicit value).",
        )
        heads.add("MISCELLANEOUS", None, {"percentage": None}, formula, version=version, status=COST_ERROR["MISC_CONFIG_INVALID"])
    elif js_number(pct) < 0 or js_number(pct) >= 100:
        heads.fail(COST_ERROR["MISC_CONFIG_INVALID"], "MISCELLANEOUS", f"Miscellaneous allowance {js_str(pct)}% is out of range. 0 <= pct < 100.")
        heads.add("MISCELLANEOUS", None, {"percentage": js_number(pct)}, formula, version=version, status=COST_ERROR["MISC_CONFIG_INVALID"])
    elif direct_exact is None:
        heads.add(
            "MISCELLANEOUS",
            None,
            {"percentage": js_number(pct), "directProjectCost": None},
            formula,
            version=version,
            status="INCOMPLETE",
            note="Direct project cost is incomplete, so the allowance cannot be computed.",
        )
    else:
        heads.add(
            "MISCELLANEOUS",
            mul_exact(direct_exact, js_number(pct) / 100),
            {"percentage": js_number(pct), "directProjectCost": round_money(direct_exact), "base": "DIRECT_PROJECT_COST_BEFORE_MISCELLANEOUS"},
            f"{js_str(round_money(direct_exact))} × {js_str(pct)}%",
            version=version,
            source=jsget(misc, "source"),
            note=("CONFIGURED MISCELLANEOUS ALLOWANCE — an explicit cost allowance, NOT hidden margin. " "Computed on direct project cost before miscellaneous, never on the selling price."),
        )


def calculate_cost(
    *,
    snapshot: Any,
    config: Any,
    price_master: Any = None,
    project: Any = None,
    rate_card: Any = None,
    calculated_at: Any = None,
    catalog_version: Any = None,
    procurement_price_version: Any = None,
    landed_cost_version: Any = None,
    project_rate_card_version: Any = None,
) -> dict:
    """``calculateCost`` → ``COMPLETE`` / ``INCOMPLETE`` / ``UNAVAILABLE`` with per-head amounts, traces and errors.

    ``snapshot``: a LOCKED BOM snapshot (``status``, ``projectId``, ``lines[{componentId, quantity, role}]``).
    ``config``: the cost configuration (``data/cost-config.json`` shape). ``price_master``: ``{componentId: record}``.
    ``project``: ``sizeKw``, ``installationType``/``roofType``, ``distanceKm``, ``vehicleType``,
    ``structureMaterialCost``, ``siteSurveyIncluded``, ``specialWorks``, ``tier``, ``rateEffectiveAt``.
    """
    project = project if isinstance(project, dict) else {}
    price_master = price_master if isinstance(price_master, dict) else {}
    if not truthy(snapshot) or jsget(snapshot, "status") != SNAPSHOT_STATUS["LOCKED"]:
        return {
            "status": COST_STATUS["UNAVAILABLE"],
            "error": COST_ERROR["COST_NOT_AVAILABLE_UNTIL_BOM_LOCKED"],
            "errors": [
                {
                    "code": COST_ERROR["COST_NOT_AVAILABLE_UNTIL_BOM_LOCKED"],
                    "head": None,
                    "message": "Cost cannot be calculated from an unlocked engineering configuration. Lock the project BOM first.",
                }
            ],
            "projectId": nullish(jsget(snapshot, "projectId"), None),
            "costEngineVersion": COST_ENGINE_VERSION,
            "heads": {},
            "totalActualProjectCost": None,
            "totalActualProjectCostExact": None,
            "calculatedAt": calculated_at,
        }
    cfg = js_or(config, {})
    size_kw = js_number(nullish(jsget(project, "sizeKw"), 0))
    effective_at = js_or(jsget(project, "rateEffectiveAt"), None)

    def from_card(rate_type: str, record_id: Any) -> dict:
        if not truthy(rate_card):
            return {"ok": False}
        return resolve_rate(rate_card, rate_type=rate_type, record_id=record_id, at=effective_at)

    heads = _Heads()
    _material(snapshot, price_master, {"landed": landed_cost_version, "procurement": procurement_price_version}, heads)
    inst_type = _structure(cfg, project, heads)
    _installation(cfg, size_kw, inst_type, from_card, heads)
    _site_survey(cfg, project, from_card, heads)
    _engineering_design(cfg, size_kw, from_card, heads)
    _transportation(cfg, project, from_card, heads)
    _service(cfg, size_kw, heads)
    _special_works(project, heads)
    _office(cfg, heads)
    direct_ok = all(heads.heads.get(h, {}).get("amountExact") is not None for h in DIRECT_COST_HEADS)
    direct_exact = sum_exact(heads.heads[h]["amountExact"] for h in DIRECT_COST_HEADS) if direct_ok else None
    _miscellaneous(cfg, direct_exact, heads)
    all_ok = all(heads.heads.get(h, {}).get("amountExact") is not None for h in COST_HEAD_ORDER)
    total_exact = sum_exact(heads.heads[h]["amountExact"] for h in COST_HEAD_ORDER) if all_ok else None
    h = heads.heads
    snapshot_id = jsget(snapshot, "snapshotId")
    if is_nullish(snapshot_id):
        snapshot_id = f"{js_str(jsget(snapshot, 'projectId'))}@{js_str(jsget(snapshot, 'lockedAt'))}"
    errors = heads.errors
    return clean(
        {
            "status": COST_STATUS["INCOMPLETE"] if errors else COST_STATUS["COMPLETE"],
            "error": errors[0]["code"] if errors else None,
            "errors": errors,
            "projectId": jsget(snapshot, "projectId"),
            "bomSnapshotId": snapshot_id,
            "tier": nullish(jsget(project, "tier"), None),
            "sizeKw": size_kw,
            "costEngineVersion": COST_ENGINE_VERSION,
            "catalogVersion": catalog_version,
            "procurementPriceVersion": procurement_price_version,
            "landedCostVersion": landed_cost_version,
            "projectRateCardVersion": nullish(project_rate_card_version, jsget(rate_card, "cardVersion"), None),
            "costConfigVersion": nullish(jsget(cfg, "configVersion"), None),
            "calculatedAt": calculated_at,
            "materialLandedCost": h["MATERIAL_LANDED"]["amount"],
            "structureCost": h["STRUCTURE"]["amount"],
            "installationCost": h["INSTALLATION"]["amount"],
            "siteSurveyCost": h["SITE_SURVEY"]["amount"],
            "engineeringDesignCost": h["ENGINEERING_DESIGN"]["amount"],
            "transportationCost": h["TRANSPORTATION"]["amount"],
            "serviceAmcCost": h["SERVICE_AMC"]["amount"],
            "specialProjectWorksCost": h["SPECIAL_PROJECT_WORKS"]["amount"],
            "officeExpenseAllocation": h["OFFICE_EXPENSE_ALLOCATION"]["amount"],
            "miscellaneousCost": h["MISCELLANEOUS"]["amount"],
            "directProjectCost": None if direct_exact is None else round_money(direct_exact),
            "directProjectCostExact": direct_exact,
            "totalActualProjectCost": None if total_exact is None else round_money(total_exact),
            "totalActualProjectCostExact": total_exact,
            "heads": h,
            "traces": [
                {
                    "head": head,
                    "owner": h[head]["owner"],
                    "inputs": h[head]["inputs"],
                    "formula": h[head]["formula"],
                    "amount": h[head]["amount"],
                    "source": h[head]["source"],
                    "version": h[head]["configVersion"],
                    "status": h[head]["status"],
                }
                for head in COST_HEAD_ORDER
            ],
        }
    )


def explain_cost(result: Any) -> str:
    """Developer text dump of a cost result."""
    if not truthy(result):
        return ""
    if result.get("status") == COST_STATUS["UNAVAILABLE"]:
        return result["error"]
    rows = []
    for trace in result["traces"]:
        row = f"{trace['head'].ljust(26)} {js_str(nullish(trace['amount'], '—')).rjust(11)}  {trace['owner'].ljust(13)} {js_str(trace['formula'])}"
        if trace["status"] != "OK":
            row += f"   [{js_str(trace['status'])}]"
        rows.append(row)
    out = [
        f"PROJECT {js_str(jsget(result, 'projectId'))}   engine={result['costEngineVersion']}   config={js_str(jsget(result, 'costConfigVersion'))}",
        *rows,
        f"{'DIRECT_PROJECT_COST'.ljust(26)} {js_str(nullish(result['directProjectCost'], '—')).rjust(11)}",
        f"{'TOTAL_ACTUAL_PROJECT_COST'.ljust(26)} {js_str(nullish(result['totalActualProjectCost'], '—')).rjust(11)}",
    ]
    if result["errors"]:
        out += [""] + [f"! {e['code']} [{js_or(e['head'], '-')}] {e['message']}" for e in result["errors"]]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------------
# Procurement batch — landed cost allocation
# ---------------------------------------------------------------------------------------------------------------


def _reconcile_to_total(exact_values: list, target: Any) -> list:
    floors = [js_floor(v) for v in exact_values]
    floor_total: int | float = 0
    for value in floors:
        floor_total = floor_total + value
    remainder = target - floor_total
    order = sorted(range(len(exact_values)), key=lambda i: (-(exact_values[i] - math.floor(exact_values[i])), i))
    out = list(floors)
    k = 0
    while k < len(order) and remainder > 0:
        out[order[k]] += 1
        k += 1
        remainder -= 1
    return out


def build_batch_landed_costs(batch: Any = None) -> dict:
    """``buildBatchLandedCosts``: landed unit cost per line = purchase price + allocated delivery/qty + other charges/qty.

    ``batch``: ``procurementBatchId``, ``deliveryCost``, ``allocationMethod`` (only PURCHASE_VALUE_PROPORTION),
    ``lines[{componentId, quantity, purchaseUnitPrice, otherProcurementCharges?[{label, amount}]}]`` and audit fields.
    """
    batch = batch if isinstance(batch, dict) else {}
    batch_id = nullish(jsget(batch, "procurementBatchId"), None)

    def fail(code: str, message: str) -> dict:
        return {"ok": False, "batchId": batch_id, "allocation": None, "lines": [], "errors": [{"code": code, "message": message, "detail": None}]}

    method = js_or(jsget(batch, "allocationMethod"), ALLOCATION_METHOD["PURCHASE_VALUE_PROPORTION"])
    if method in FORBIDDEN_ALLOCATION_METHODS:
        return fail(BATCH_ERROR["ALLOCATION_METHOD_INVALID"], f"Allocation by {js_str(method)} is not permitted. The approved method is PURCHASE_VALUE_PROPORTION.")
    if method != ALLOCATION_METHOD["PURCHASE_VALUE_PROPORTION"]:
        return fail(BATCH_ERROR["ALLOCATION_METHOD_INVALID"], f'Unknown allocation method "{js_str(method)}". Only PURCHASE_VALUE_PROPORTION is approved.')
    if not is_nullish(jsget(batch, "projectTransportCost")):
        return fail(
            BATCH_ERROR["PROJECT_TRANSPORT_NOT_ALLOWED"],
            "Project transportation (warehouse -> site) must never be allocated into landed cost. It is a separate project cost head.",
        )
    raw_lines = list(js_or(jsget(batch, "lines"), []))
    raw_lines.sort(key=lambda line: js_str_key(js_str(js_or(jsget(line, "componentId"), ""))))
    errors = []
    for line in raw_lines:
        if not truthy(jsget(line, "componentId")) or not is_money(jsget(line, "quantity")) or js_number(line["quantity"]) <= 0 or not is_money(jsget(line, "purchaseUnitPrice")):
            errors.append(
                {
                    "code": BATCH_ERROR["BATCH_LINE_INVALID"],
                    "message": f'Batch line "{js_str(nullish(jsget(line, "componentId"), "(no id)"))}" needs a componentId, a positive quantity and a purchaseUnitPrice.',
                    "detail": line,
                }
            )
    if errors:
        return {"ok": False, "batchId": batch_id, "allocation": None, "lines": [], "errors": errors}
    valued = [
        {
            **line,
            "quantity": js_number(line["quantity"]),
            "purchaseUnitPrice": js_number(line["purchaseUnitPrice"]),
            "purchaseValueExact": mul_exact(line["quantity"], line["purchaseUnitPrice"]),
        }
        for line in raw_lines
    ]
    total_purchase = sum_exact(line["purchaseValueExact"] for line in valued)
    delivery_cost = js_number(batch["deliveryCost"]) if is_money(jsget(batch, "deliveryCost")) else 0
    if delivery_cost > 0 and not total_purchase > 0:
        return fail(BATCH_ERROR["ZERO_PURCHASE_VALUE"], "A delivery charge cannot be allocated by purchase value when the batch purchase value is zero.")
    shares = [line["purchaseValueExact"] / total_purchase if total_purchase > 0 else 0 for line in valued]
    allocated = _reconcile_to_total([delivery_cost * share for share in shares], round_money(delivery_cost))
    lines = []
    for line, allocated_amount in zip(valued, allocated):
        others = [c for c in js_or(jsget(line, "otherProcurementCharges"), []) if is_money(jsget(c, "amount"))]
        others_exact = sum_exact(js_number(c["amount"]) for c in others)
        landed_total = line["purchaseValueExact"] + allocated_amount + others_exact
        landed_unit = landed_total / line["quantity"]
        lines.append(
            {
                "componentId": line["componentId"],
                "quantity": line["quantity"],
                "purchaseUnitPrice": line["purchaseUnitPrice"],
                "purchaseValue": round_money(line["purchaseValueExact"]),
                "allocationPct": js_round((line["purchaseValueExact"] / total_purchase) * 1e6) / 1e4 if total_purchase > 0 else 0,
                "allocatedDeliveryCost": allocated_amount,
                "otherProcurementCharges": [{"label": nullish(jsget(c, "label"), None), "amount": round_money(js_number(c["amount"]))} for c in others],
                "otherProcurementChargesTotal": round_money(others_exact),
                "landedTotalCost": round_money(landed_total),
                "landedUnitCost": round_money(landed_unit),
                "landedUnitCostExact": landed_unit,
                "landedCostSource": "PROCUREMENT_BATCH_ALLOCATION",
                "procurementBatchId": batch_id,
                "formula": f"{js_str(line['purchaseUnitPrice'])} + ({js_str(allocated_amount)} + {js_str(round_money(others_exact))}) / {js_str(line['quantity'])}",
            }
        )
    allocated_total = sum_exact(allocated)
    return {
        "ok": True,
        "batchId": batch_id,
        "allocation": {
            "procurementBatchId": batch_id,
            "deliveryCost": round_money(delivery_cost),
            "allocationMethod": ALLOCATION_METHOD["PURCHASE_VALUE_PROPORTION"],
            "scope": "SUPPLIER_TO_WAREHOUSE",
            "totalPurchaseValue": round_money(total_purchase),
            "allocatedTotal": allocated_total,
            "reconciled": allocated_total == round_money(delivery_cost),
            "sourceMaterials": [
                {"componentId": line["componentId"], "purchaseValue": line["purchaseValue"], "allocationPct": line["allocationPct"], "allocatedAmount": line["allocatedDeliveryCost"]} for line in lines
            ],
            "recordedBy": nullish(jsget(batch, "recordedBy"), None),
            "recordedAt": nullish(jsget(batch, "recordedAt"), None),
            "effectiveFrom": nullish(jsget(batch, "effectiveFrom"), None),
            "versionId": nullish(jsget(batch, "versionId"), None),
        },
        "lines": lines,
        "errors": [],
    }


def allocate_landed(
    lines: Iterable[dict],
    charges: Iterable[dict] = (),
    *,
    batch_id: Any = None,
    allocation_method: Any = ALLOCATION_METHOD["PURCHASE_VALUE_PROPORTION"],
    recorded_by: Any = None,
    recorded_at: Any = None,
    effective_from: Any = None,
    version_id: Any = None,
) -> dict:
    """Landed-cost allocation of a procurement batch with its batch-level charges (PLAN §2.4 ``engines.cost.allocate_landed``).

    ``charges`` are the batch's ``procurement_batch_charge`` rows (``{kind, amount}``; kinds FREIGHT, INSURANCE,
    HANDLING, DUTY, OTHER — all supplier → warehouse). Their exact sum is allocated by purchase-value proportion in one
    reconciliation, which is exactly Flarize's ``buildBatchLandedCosts`` with ``deliveryCost`` = that sum; the result
    has the same shape. A charge with an unknown kind or a missing/negative amount refuses the batch
    (``BATCH_CHARGE_INVALID``) — the JS had a single delivery figure and never saw one.
    """
    charges = list(charges or [])
    errors = []
    for charge in charges:
        kind = jsget(charge, "kind")
        amount = jsget(charge, "amount")
        if kind not in BATCH_CHARGE_KINDS or not is_money(amount) or js_number(amount) < 0:
            errors.append(
                {
                    "code": BATCH_ERROR["BATCH_CHARGE_INVALID"],
                    "message": f'Batch charge "{js_str(kind)}" needs a kind in {", ".join(BATCH_CHARGE_KINDS)} and an amount ≥ 0.',
                    "detail": charge,
                }
            )
    if errors:
        return {"ok": False, "batchId": batch_id, "allocation": None, "lines": [], "errors": errors}
    return build_batch_landed_costs(
        {
            "procurementBatchId": batch_id,
            "deliveryCost": sum_exact(jsget(c, "amount") for c in charges),
            "allocationMethod": allocation_method,
            "lines": list(lines or []),
            "recordedBy": recorded_by,
            "recordedAt": recorded_at,
            "effectiveFrom": effective_from,
            "versionId": version_id,
        }
    )


def to_price_master_entries(result: Any, *, purchase_price_version: Any = None, landed_cost_version: Any = None, supplier: Any = None) -> dict:
    """Price-master records (purchase price and landed cost kept separate) from a batch result."""
    if not truthy(jsget(result, "ok")):
        return {}
    out = {}
    for line in result["lines"]:
        out[line["componentId"]] = {
            "purchasePrice": line["purchaseUnitPrice"],
            "purchasePriceVersion": purchase_price_version,
            "landedUnitCost": line["landedUnitCost"],
            "landedCostVersion": landed_cost_version,
            "supplier": supplier,
            "procurementBatchId": line["procurementBatchId"],
            "landedCostBuildUp": {
                "purchaseUnitPrice": line["purchaseUnitPrice"],
                "allocatedDeliveryCost": line["allocatedDeliveryCost"],
                "otherProcurementChargesTotal": line["otherProcurementChargesTotal"],
                "allocationMethod": ALLOCATION_METHOD["PURCHASE_VALUE_PROPORTION"],
                "allocationPct": line["allocationPct"],
                "formula": line["formula"],
            },
        }
    return out
