"""Pack pricing — the owner's v1 quotation formula (``PACK_MARKET_RATE``), port of Flarize ``src/lib/packPricing.js``.

    customerPriceInclGst = marketRate[pack][panel size]            (Project Head, GST-inclusive)
                         + Σ swapDelta  (selected − PH default, at the line's own GST rate)
                         + roofAddOn    (non-flat roofs: installation diff + structure material diff + labour + repair %)
    transportExtra       = max(0, distanceKm − baseDistanceKm) × vehicle ₹/km   (a separate customer extra, no GST)
    preGst / gst         = split of customerPriceInclGst at the composite rate (70 % × 5 % + 30 % × 18 % = 8.9 %)

Also an internal reference check (Project Head / Admin only) comparing the market rate with catalog reference cost
plus margin. Missing market rate, installation cell, vehicle, distance or roof → ``BLOCKED`` with every error
collected; nothing is defaulted.
"""

from __future__ import annotations

import math
import re
from typing import Any

from engines._jscompat import (
    clean,
    is_array,
    is_nullish,
    is_num,
    js_add,
    js_ceil,
    js_keys,
    js_max,
    js_number,
    js_or,
    js_round,
    js_str,
    js_to_fixed,
    jsget,
    nullish,
    strict_equal,
    truthy,
)
from engines.pack_config import ROOF_TO_STRUCTURE_KEY, market_rate_key

PACK_PRICING_VERSION = "pack-pricing.1"
PACK_PRICING_STATUS = {"COMPLETE": "COMPLETE", "BLOCKED": "BLOCKED"}
PACK_PRICING_ERROR = {
    "MARKET_RATE_NOT_SET": "MARKET_RATE_NOT_SET",
    "INSTALLATION_NOT_SET": "INSTALLATION_NOT_SET",
    "VEHICLE_NOT_SET": "VEHICLE_NOT_SET",
    "DISTANCE_INVALID": "DISTANCE_INVALID",
    "ROOF_INVALID": "ROOF_INVALID",
}
DEFAULT_GST_RATE_PCT = 8.9
DEFAULT_BASE_DISTANCE_KM = 100
REFERENCE_NOTE = "Reference cost uses catalog prices, not procurement landed cost. Compare with the Cost Engine (landed) when it is complete."

r0 = js_round


def structure_qty(item: Any, kw: Any) -> Any:
    """Quantity for a kW from a ``{size: qty}`` map: exact/below first, scaled above the last, interpolated between."""
    qty_map = js_or(jsget(item, "qty"), {})
    sizes = sorted(n for n in (js_number(k) for k in js_keys(qty_map)) if math.isfinite(n))
    if not sizes:
        return 0
    kw = js_number(kw)

    def at(size: Any) -> Any:
        return jsget(qty_map, js_str(size))

    if kw <= sizes[0]:
        return at(sizes[0])
    last = sizes[-1]
    if kw >= last:
        return js_ceil(js_number(at(last)) * kw / last)
    for low, high in zip(sizes, sizes[1:]):
        if low <= kw <= high:
            ratio = (kw - low) / (high - low)
            return js_ceil(js_add(at(low), ratio * (js_number(at(high)) - js_number(at(low)))))
    return at(last)


def structure_material(template: Any, kw: Any, rate_per_kg: Any) -> dict:
    """Material lines and total for one roof template at a kW and tube rate (₹/kg); every figure rounded."""
    items = jsget(template, "items")
    if not truthy(template) or not is_array(items):
        return {"total": 0, "lines": []}
    total: int | float = 0
    lines = []
    for item in items:
        qty = structure_qty(item, kw)
        if not truthy(qty):
            continue
        is_tube = jsget(item, "type") == "tube"
        if is_tube:
            unit_price = r0(js_number(js_or(jsget(item, "weightKg"), 0)) * js_number(rate_per_kg))
        else:
            unit_price = r0(js_or(jsget(item, "price"), 0))
        amount = r0(unit_price * js_number(qty))
        total += amount
        lines.append(
            clean(
                {
                    "name": jsget(item, "name"),
                    "type": jsget(item, "type"),
                    "tubeSize": nullish(jsget(item, "tubeSize"), None),
                    "weightKg": nullish(jsget(item, "weightKg"), None),
                    "qty": qty,
                    "unitPrice": unit_price,
                    "amount": amount,
                    "isTube": is_tube,
                }
            )
        )
    return {"total": total, "lines": lines}


def tube_rate_for(costs: Any, tier: Any) -> Any:
    """Tube ₹/kg for a tier: Base = GP (``gpRatePerKg``, 85), Value/Premium = GI (``giRatePerKg``, 98.3)."""
    gp = nullish(jsget(costs, "gpRatePerKg"), 85)
    gi = nullish(jsget(costs, "giRatePerKg"), 98.3)
    return gp if tier == "base" else gi


def kw_of(size: Any) -> int | float:
    """kW number of a size key (``'5sp'`` → 5)."""
    number = js_number(re.sub(r"[^0-9.]", "", js_str(size)))
    return number if truthy(number) else 0


def _swap_deltas(lines: Any) -> tuple[list, int | float]:
    deltas = []
    total: int | float = 0
    for line in lines if truthy(lines) else []:
        default_id = jsget(line, "defaultComponentId")
        if not truthy(jsget(line, "isVariable")) or not truthy(default_id) or strict_equal(default_id, jsget(line, "componentId")):
            continue
        selected_total = js_number(js_or(jsget(line, "unitPrice"), 0)) * js_number(js_or(jsget(line, "qty"), 0))
        default_total = js_number(js_or(jsget(line, "defaultUnitPrice"), 0)) * js_number(nullish(jsget(line, "defaultQty"), jsget(line, "qty"), 0))
        diff_pre_gst = r0(selected_total - default_total)
        diff_gst = r0(diff_pre_gst * js_number(js_or(jsget(line, "gst"), 0)) / 100)
        diff = diff_pre_gst + diff_gst
        if diff != 0:
            total += diff
            deltas.append(
                clean(
                    {
                        "category": jsget(line, "category"),
                        "selected": jsget(line, "componentId"),
                        "selectedName": nullish(jsget(line, "name"), None),
                        "default": default_id,
                        "defaultName": nullish(jsget(line, "defaultName"), None),
                        "qty": jsget(line, "qty"),
                        "diffPreGst": diff_pre_gst,
                        "diffGst": diff_gst,
                        "diff": diff,
                        "gstPct": nullish(jsget(line, "gst"), None),
                    }
                )
            )
    return deltas, total


def price_pack(
    *,
    config: Any,
    system_type: Any,
    size: Any,
    tier: Any,
    roof_type: Any,
    distance_km: Any,
    vehicle_type: Any = None,
    battery_config: Any = None,
    future_system_size: Any = None,
    lines: Any = None,
    priced_at: Any = None,
) -> dict:
    """``pricePack`` → ``COMPLETE`` (customer price, swap deltas, roof add-on, transport extra, internal check) or ``BLOCKED``."""
    errors: list[dict] = []
    cfg = js_or(config, {})
    costs = js_or(jsget(cfg, "costs"), {})
    gst_rate = jsget(jsget(cfg, "gst"), "ratePct")
    gst_pct = gst_rate if is_num(gst_rate) else DEFAULT_GST_RATE_PCT
    roof = js_str(js_or(roof_type, "")).upper()
    if not truthy(jsget(ROOF_TO_STRUCTURE_KEY, roof)):
        errors.append({"code": PACK_PRICING_ERROR["ROOF_INVALID"], "message": f'roofType "{js_str(roof_type)}" must be FLAT | SHEET | ELEVATED.'})
    dist = js_number(distance_km)
    if not is_num(dist) or dist < 0:
        errors.append({"code": PACK_PRICING_ERROR["DISTANCE_INVALID"], "message": "distanceKm must be a number ≥ 0."})

    key = market_rate_key(system_type=system_type, tier=tier, battery_config=nullish(battery_config, 0), future_system_size=js_or(future_system_size, None))
    market_rate = jsget(jsget(jsget(cfg, "marketRates"), key), js_str(size))
    if not is_num(market_rate) or market_rate <= 0:
        errors.append(
            {
                "code": PACK_PRICING_ERROR["MARKET_RATE_NOT_SET"],
                "message": f"Market rate not set for {key} / {js_str(size)}. Project Head must publish it.",
                "detail": {"key": key, "size": js_str(size)},
            }
        )

    sys_size = js_str(js_or(future_system_size, size))
    installation = jsget(jsget(cfg, "installationMatrix"), sys_size)
    install_flat = jsget(installation, "flat") if truthy(installation) else None
    install_roof = jsget(installation, roof.lower()) if truthy(installation) else None
    if not is_num(install_flat) or not is_num(install_roof):
        errors.append(
            {
                "code": PACK_PRICING_ERROR["INSTALLATION_NOT_SET"],
                "message": f"Installation amount not set for {sys_size} kW / {roof}.",
                "detail": {"size": sys_size, "roof": roof},
            }
        )

    transport = js_or(jsget(cfg, "transportConfig"), {})
    base_km_value = jsget(transport, "baseDistanceKm")
    base_km = base_km_value if is_num(base_km_value) else DEFAULT_BASE_DISTANCE_KM
    vehicles = jsget(transport, "vehicles")
    vehicles = vehicles if is_array(vehicles) else []
    if truthy(vehicle_type):
        vehicle = next((v for v in vehicles if strict_equal(jsget(v, "vehicleType"), vehicle_type)), None)
    else:
        vehicle = vehicles[0] if len(vehicles) == 1 else None
    if not truthy(vehicle):
        errors.append(
            {
                "code": PACK_PRICING_ERROR["VEHICLE_NOT_SET"],
                "message": (f'Vehicle "{js_str(vehicle_type)}" is not in the Project Head transport table.' if truthy(vehicle_type) else "Vehicle type required (more than one vehicle configured)."),
                "detail": {"configured": [nullish(jsget(v, "vehicleType"), None) for v in vehicles]},
            }
        )

    if errors:
        return {
            "status": PACK_PRICING_STATUS["BLOCKED"],
            "errors": errors,
            "packPricingVersion": PACK_PRICING_VERSION,
            "marketRateKey": key,
            "pricedAt": nullish(priced_at, None),
        }

    swap_deltas, swap_delta_total = _swap_deltas(lines)

    kw = kw_of(sys_size)
    tube_rate = tube_rate_for(costs, tier)
    templates = js_or(jsget(cfg, "structureTemplates"), {})
    flat = structure_material(jsget(templates, ROOF_TO_STRUCTURE_KEY["FLAT"]), kw, tube_rate)
    is_flat = roof == "FLAT"
    chosen = flat if is_flat else structure_material(jsget(templates, ROOF_TO_STRUCTURE_KEY[roof]), kw, tube_rate)
    structure_extra = 0 if is_flat else js_max(0, chosen["total"] - flat["total"])
    structure_labour = 0 if is_flat else r0(js_or(jsget(costs, "structureLabor"), 0))
    repair_pct = js_number(js_or(jsget(costs, "structureRepairPct"), 0))
    structure_repair = 0 if is_flat else r0((structure_extra + structure_labour) * repair_pct / 100)
    installation_diff = 0 if is_flat else r0(install_roof - install_flat)
    roof_add_on_pre_gst = structure_extra + structure_labour + structure_repair + installation_diff
    roof_add_on_gst = r0(roof_add_on_pre_gst * gst_pct / 100)
    roof_add_on = roof_add_on_pre_gst + roof_add_on_gst

    customer_price_incl_gst = r0(market_rate + swap_delta_total + roof_add_on)
    pre_gst = r0(customer_price_incl_gst / (1 + gst_pct / 100))
    gst_amount = customer_price_incl_gst - pre_gst

    rate_per_km = jsget(vehicle, "ratePerKm")
    extra_km = js_max(0, dist - base_km)
    transport_extra = r0(extra_km * js_number(rate_per_km))

    material: int | float = 0
    for line in lines if truthy(lines) else []:
        amount = jsget(line, "amount")
        if is_nullish(amount):
            amount = js_number(js_or(jsget(line, "unitPrice"), 0)) * js_number(js_or(jsget(line, "qty"), 0))
        material = js_add(material, amount)
    transport_rate = jsget(costs, "transportRate")
    reference_cost = {
        "material": r0(material),
        "structureMaterial": r0(chosen["total"]),
        "installation": r0(install_roof),
        "service": r0(js_number(js_or(jsget(costs, "serviceRateYear"), 0)) * js_number(js_or(jsget(costs, "serviceYears"), 0))),
        "transportationBase": r0(base_km * js_number(transport_rate if is_num(transport_rate) else rate_per_km)),
        "miscellaneous": r0(js_or(jsget(costs, "miscellaneous"), 0)),
        "office": r0(js_or(jsget(costs, "office"), 0)),
        "structureLabour": structure_labour,
        "structureRepair": structure_repair,
    }
    reference_total: int | float = 0
    for value in reference_cost.values():
        reference_total = reference_total + value
    margin_value = jsget(jsget(cfg, "pricing"), "marginPct")
    margin_pct = margin_value if is_num(margin_value) else 0
    margin_amt = r0(reference_total * margin_pct / 100)
    grand_pre_gst = reference_total + margin_amt
    grand = grand_pre_gst + r0(grand_pre_gst * gst_pct / 100)

    vehicle_label = jsget(vehicle, "vehicleType")
    return {
        "status": PACK_PRICING_STATUS["COMPLETE"],
        "packPricingVersion": PACK_PRICING_VERSION,
        "pricedAt": nullish(priced_at, None),
        "marketRateKey": key,
        "inputs": clean(
            {
                "systemType": system_type,
                "size": js_str(size),
                "systemSize": sys_size,
                "tier": tier,
                "roofType": roof,
                "distanceKm": dist,
                "vehicleType": vehicle_label,
                "batteryConfig": nullish(battery_config, 0),
                "futureSystemSize": js_or(future_system_size, None),
            }
        ),
        "gst": {"regime": js_or(jsget(jsget(cfg, "gst"), "regime"), "SOLAR_70_30_COMPOSITE"), "ratePct": gst_pct},
        "customer": {
            "marketRate": r0(market_rate),
            "swapDeltaTotal": r0(swap_delta_total),
            "roofAddOn": roof_add_on,
            "roofAddOnPreGst": roof_add_on_pre_gst,
            "roofAddOnGst": roof_add_on_gst,
            "sellingPriceBeforeGST": pre_gst,
            "gstAmount": gst_amount,
            "sellingPriceIncludingGST": customer_price_incl_gst,
            "transportExtra": transport_extra,
            "transportExtraDetail": clean({"distanceKm": dist, "includedKm": base_km, "extraKm": extra_km, "ratePerKm": rate_per_km, "vehicleType": vehicle_label}),
            "customerTotalIncludingGST": customer_price_incl_gst + transport_extra,
        },
        "swapDeltas": swap_deltas,
        "roofAddOnDetail": {
            "roofType": roof,
            "installationFlat": r0(install_flat),
            "installationRoof": r0(install_roof),
            "installationDiff": installation_diff,
            "structureFlatTotal": r0(flat["total"]),
            "structureRoofTotal": r0(chosen["total"]),
            "structureExtra": structure_extra,
            "structureLabour": structure_labour,
            "structureRepair": structure_repair,
            "tubeRatePerKg": tube_rate,
        },
        "structureMaterial": {"roofType": roof, "kw": kw, "tubeRatePerKg": tube_rate, "lines": chosen["lines"], "total": r0(chosen["total"])},
        "internal": {
            "referenceCost": reference_cost,
            "referenceTotal": r0(reference_total),
            "marginPct": margin_pct,
            "marginAmt": margin_amt,
            "grandPreGst": r0(grand_pre_gst),
            "grand": r0(grand),
            "marginVsMarket": r0(customer_price_incl_gst - grand),
            "marginVsMarketPct": js_number(js_to_fixed((customer_price_incl_gst - grand) / grand * 100, 1)) if grand > 0 else None,
            "note": REFERENCE_NOTE,
        },
    }
