"""Package BOM materialisation — port of Flarize ``server-bom-builder.js``.

Given ``(systemType, size, tier, phase, selections, batteryQuantity, futureSystemSize)``, build priced BOM lines from
the catalog, overlaid with the Project Head's pack configuration, and pinned by the package registry. The JS read
``data/catalog.json`` and ``data/packages.proposed.json`` itself; here both arrive as dict inputs:

``catalog``      the Flarize catalog (categories, templates, profiles …) or an equivalent mapping built by a service
``pack_config``  the pack configuration for the requested ``configSource`` (``approved`` / ``draft``): a dict, a
                 callable ``source -> dict | None`` (the JS ``setPackConfigProvider``), or ``None`` (catalog only)
``registry``     ``{"packages": [...]}`` — the approved-package registry used for default pins and approved alternates

Rules (in order): the Future Ready split (panels on ``size``, everything else on ``futureSystemSize``), hard
``UNSUPPORTED_CONFIGURATION`` blocks, project battery quantity, slot quantities (premium / battery / plain maps), the
Project Head default → registry pin → nearest-kW inverter → first eligible item, Sales swap rules, panel count from
wattage, Enphase accessories through :func:`engines.device_allocation.allocate_devices`, and fixed consumables.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from engines._jscompat import (
    JsError,
    clean,
    includes,
    is_array,
    is_integer,
    is_nullish,
    is_number,
    js_ceil,
    js_div,
    js_join,
    js_max,
    js_number,
    js_or,
    js_round,
    js_str,
    jsget,
    nullish,
    parse_float,
    strict_equal,
    truthy,
)
from engines.device_allocation import allocate_devices
from engines.flarize_rbac import ALL_ROLES

PACK_CONFIG_SECTIONS = (
    "bomTemplates",
    "structureTemplates",
    "tubeWeights",
    "costs",
    "installationMatrix",
    "transportConfig",
    "marketRates",
    "gst",
    "pricing",
    "futureUpgrade",
)
INDIVIDUAL_SALES_ROLES = ("SALES", "SALES_CRS", "FIELD_SALES")
SALES_SELECTABLE_CATEGORIES = ("panel", "inverter", "structure_material")
# DV-25: ``defaultUnitPrice`` is stripped too — the JS left it, and on every unswapped line it IS the unitPrice.
SENSITIVE_LINE_FIELDS = ("unitPrice", "amount", "gst", "gstAmt", "defaultUnitPrice", "landedUnitCost", "purchasePrice", "supplier")

PackConfigSource = Mapping | Callable[[str], Any] | None


class BomBuildError(JsError):
    """What ``buildBom`` throws: ``UNSUPPORTED_CONFIGURATION``, ``SELECTION_NOT_PERMITTED``, ``SELECTION_NOT_APPROVED``
    or a plain error (``code`` ``None``) for an unknown template, size, tier or component."""

    js_name = "Error"


def load_catalog(catalog: Mapping, pack_config: PackConfigSource = None, source: str = "approved") -> dict:
    """``loadCatalog``: the catalog with the pack-config sections laid over it; ``_packConfigSource`` records which."""
    effective = dict(catalog or {})
    config = pack_config(source) if callable(pack_config) else pack_config
    if truthy(config):
        for key in PACK_CONFIG_SECTIONS:
            if not is_nullish(jsget(config, key)):
                effective[key] = config[key]
    effective["_packConfigSource"] = source if truthy(config) else "CATALOG_ONLY"
    return effective


def is_sales_role(role: Any) -> bool:
    return role in INDIVIDUAL_SALES_ROLES or role == "SALES_HEAD"


def held_to_sales_rules(role: Any) -> bool:
    """A Sales role, or any role the Flarize engines do not know (DV-26: least privilege).

    ``actorRole`` speaks Flarize's ``rbac.1`` vocabulary (``SALES``, ``PROJECT_HEAD`` …). The JS let every other string
    through with Project Head powers — a platform role slug such as ``sales-executive`` could swap any component and
    read reference prices. Unknown roles now get the Sales view; only a *missing* role keeps the internal, unrestricted
    build the JS gave it (pack publishing, catalog builds).
    """
    return is_sales_role(role) or role not in ALL_ROLES


def get_profile_key(system_type: Any, tier: Any) -> str:
    """``hybrid_<tier>``, ``ongrid_premium`` or ``ongrid_<tier>`` (never the global ``premium`` profile)."""
    if system_type == "hybrid":
        return f"hybrid_{js_str(tier)}"
    if tier == "premium":
        return "ongrid_premium"
    return f"ongrid_{js_str(tier)}"


def strip_cost_fields_for_sales(lines: list[dict]) -> list[dict]:
    """Remove prices, GST and procurement fields from lines returned to Sales roles."""
    return [{k: v for k, v in line.items() if k not in SENSITIVE_LINE_FIELDS} for line in lines]


def _find_registry_package(registry: Any, *, system_type: Any, size: Any, tier: Any, phase: Any) -> dict | None:
    packages = jsget(registry, "packages")
    for package in packages if truthy(packages) else []:
        if (
            strict_equal(jsget(package, "systemType"), system_type)
            and strict_equal(jsget(package, "size"), size)
            and strict_equal(jsget(package, "tier"), tier)
            and strict_equal(jsget(package, "phase"), phase)
        ):
            return package
    return None


def _approved_alternate_ids(registry_pkg: Any, role: Any) -> Any:
    if not truthy(registry_pkg):
        return None
    components = jsget(registry_pkg, "components")
    comp = next((c for c in (components if truthy(components) else []) if strict_equal(jsget(c, "role"), role)), None)
    if comp is None:
        return None
    alternates = jsget(comp, "approvedAlternates")
    return alternates if truthy(alternates) else []


def _ph_default_for(slot: Any, tier: Any, size: str) -> Any:
    defaults = jsget(slot, "defaults")
    if not truthy(defaults) or not isinstance(defaults, (dict, list)):
        return None
    return nullish(
        jsget(defaults, f"{js_str(tier)}:{size}"),
        jsget(defaults, size),
        jsget(defaults, f"{js_str(tier)}:*"),
        jsget(defaults, "*"),
        None,
    )


def _slot_sales_swappable(slot: Any) -> bool:
    swap = jsget(slot, "salesSwap")
    if isinstance(swap, bool):
        return swap
    return jsget(slot, "category") in SALES_SELECTABLE_CATEGORIES


def _phase_filter(slot: Any, is_three_phase: bool) -> Callable[[Any], bool]:
    filter_type = jsget(slot, "filterType")
    filter_phase = jsget(slot, "filterPhase")

    def accept(item: Any) -> bool:
        item_type = jsget(item, "type")
        phase = jsget(item, "phase")
        if filter_type == "hybrid":
            return item_type == "hybrid"
        if filter_type == "ongrid" and truthy(item_type) and item_type != "ongrid":
            return False
        if filter_phase == "HYB":
            if not includes(phase, "HYB"):
                return False
            return includes(phase, "3P") if is_three_phase else includes(phase, "1P")
        if is_three_phase and truthy(phase):
            return phase == "3P" or includes(phase, "3P")
        if not is_three_phase and truthy(phase):
            return phase == "1P" or includes(phase, "1P") or not truthy(phase)
        return True

    return accept


def _status_active(item: Any) -> bool:
    return js_or(jsget(item, "status"), "ACTIVE") == "ACTIVE"


def _nearest_kw(available: list, target_kw: Any) -> Any:
    def distance(item: Any) -> Any:
        return abs(js_number(js_or(jsget(item, "kw"), 0)) - target_kw)

    return min(available, key=distance)  # first of the minimal items — JS stable sort then [0]


def _qty_from(maps: Any, bat_config: str, size: str) -> Any:
    chosen = js_or(jsget(maps, bat_config), jsget(maps, "0"), {})
    return js_or(jsget(chosen, size), 0)


def _slot_qty(slot: Any, *, premium_micro: bool, bat_config: str, q_size: str) -> Any:
    premium_bat_qty = jsget(slot, "premiumBatQty")
    premium_qty = jsget(slot, "premiumQty")
    if premium_micro and (truthy(premium_bat_qty) or truthy(premium_qty)):
        if truthy(premium_bat_qty):
            return _qty_from(premium_bat_qty, bat_config, q_size)
        return js_or(jsget(premium_qty, q_size), 0)
    if truthy(jsget(slot, "batQty")):
        return _qty_from(jsget(slot, "batQty"), bat_config, q_size)
    if truthy(jsget(slot, "qty")):
        return js_or(jsget(jsget(slot, "qty"), q_size), 0)
    return 0


def _is_zero(value: Any) -> bool:
    return is_number(value) and value == 0


def _project_battery_quantity(raw: Any) -> Any:
    return raw if is_number(raw) and raw in (0, 1, 2) else None


def _resolve_battery_quantity(system_type: Any, project_bat_qty: Any, profile: Any) -> Any:
    if system_type != "hybrid":
        return 0
    if project_bat_qty is not None:
        return project_bat_qty
    if truthy(jsget(profile, "batteryIncluded")):
        return js_or(jsget(profile, "batteryQuantity"), 1)
    return 0


def _check_unsupported(system_type: Any, size: Any, requested_phase: Any) -> None:
    if system_type == "ongrid" and js_str(size) == "3" and requested_phase == "3P":
        raise BomBuildError(
            "Unsupported configuration: 3 kW 3-phase on-grid is not a supported system. 3 kW is single-phase only. " "To size a 3-phase system, choose 5 kW (5tp), 6 kW, 8 kW or 10 kW.",
            "UNSUPPORTED_CONFIGURATION",
            {"systemType": system_type, "size": size, "phase": requested_phase, "supportedThreePhaseSizes": ["5tp", "6", "8", "10"]},
        )
    if system_type == "hybrid" and js_str(size) == "3" and requested_phase == "3P":
        raise BomBuildError(
            "Unsupported configuration: 3 kW 3-phase hybrid is not a supported system. Hybrid 3 kW is single-phase only.",
            "UNSUPPORTED_CONFIGURATION",
            {"systemType": system_type, "size": size, "phase": requested_phase, "supportedHybrid3P": ["8", "10"]},
        )


def _selection_error(category: dict, selection_id: Any, slot_category: Any, tier: Any, accept: Callable[[Any], bool], is_three_phase: bool) -> BomBuildError:
    items = jsget(category, "items")
    existing = next((i for i in (items if truthy(items) else []) if strict_equal(jsget(i, "id"), selection_id)), None)
    if existing is None:
        return BomBuildError(f'Selected component "{js_str(selection_id)}" not found in category "{js_str(slot_category)}"')
    reasons = []
    if not includes(jsget(existing, "tiers"), tier):
        reasons.append(f"not available in {js_str(tier)} tier")
    if not _status_active(existing):
        reasons.append("not active")
    if not accept(existing):
        reasons.append(f"not compatible with {'3-phase' if is_three_phase else '1-phase'} configuration")
    name = js_or(jsget(existing, "name"), selection_id)
    return BomBuildError(f'Component "{js_str(name)}" cannot be selected for {js_str(slot_category)}: {", ".join(reasons) or "not eligible for this configuration"}')


def build_bom(config: Mapping, *, catalog: Mapping, registry: Any = None, pack_config: PackConfigSource = None) -> dict:
    """``buildBom(config)`` → ``{lines, totals, profile, systemConfig, engineering}``.

    ``config``: ``systemType``, ``size``, ``tier``, optional ``phase``, ``selections`` (``{category: componentId}``),
    ``actorRole`` (Sales roles are held to the swap rules), ``batteryQuantity`` (0/1/2), ``futureSystemSize`` and
    ``configSource`` (``approved`` | ``draft``). Raises :class:`BomBuildError`.
    """
    source = js_or(jsget(config, "configSource"), "approved")
    cat = load_catalog(catalog, pack_config, source)
    system_type = jsget(config, "systemType")
    size = jsget(config, "size")
    tier = jsget(config, "tier")
    actor_role = jsget(config, "actorRole")
    selections = js_or(jsget(config, "selections"), {})
    future = jsget(config, "futureSystemSize")
    sys_size = js_str(future) if truthy(future) else js_str(size)
    size_str = js_str(size)
    is_future_ready = sys_size != size_str
    blockers: list[dict] = []
    requested_phase = js_or(jsget(config, "phase"), None)
    _check_unsupported(system_type, size, requested_phase)

    template = jsget(jsget(cat, "bomTemplates"), system_type)
    if not truthy(template):
        raise BomBuildError(f'No BOM template for system type "{js_str(system_type)}"')
    sizes = jsget(template, "sizes")
    if not truthy(jsget(sizes, size)):
        raise BomBuildError(f'Invalid size "{size_str}" for system type "{js_str(system_type)}"')
    if is_future_ready and not truthy(jsget(sizes, sys_size)):
        raise BomBuildError(f'Invalid future-ready system size "{sys_size}" for system type "{js_str(system_type)}"', "UNSUPPORTED_CONFIGURATION")

    profile_key = get_profile_key(system_type, tier)
    profile = js_or(jsget(jsget(cat, "packageProfiles"), profile_key), None)
    three_phase = jsget(template, "threePhase")
    is_three_phase = jsget(config, "phase") == "3P" or includes(three_phase, sys_size)
    if is_future_ready and includes(three_phase, sys_size) != includes(three_phase, size_str):
        raise BomBuildError(f"Future-ready pair {size_str} → {sys_size} changes the phase; not allowed.", "UNSUPPORTED_CONFIGURATION")
    tiers = js_or(jsget(template, "tiers"), ["base", "value", "premium"])
    if not includes(tiers, tier):
        raise BomBuildError(f'Invalid tier "{js_str(tier)}". Valid: {js_join(tiers, ", ")}')

    registry_pkg = _find_registry_package(registry, system_type=system_type, size=sys_size, tier=tier, phase="3P" if is_three_phase else "1P")
    project_bat_qty = _project_battery_quantity(jsget(config, "batteryQuantity"))
    bat_qty = _resolve_battery_quantity(system_type, project_bat_qty, profile)
    bat_config = js_str(bat_qty)
    if project_bat_qty is not None:
        battery_source = "PROJECT_HEAD_OVERRIDE"
    else:
        battery_source = "PROFILE_DEFAULT" if truthy(jsget(profile, "batteryIncluded")) else "NONE"

    state = {"lines": [], "matTotal": 0, "allGst": 0, "numPanels": 0}
    premium_micro = tier == "premium" and system_type in ("ongrid", "hybrid")
    _variable_slots(
        cat,
        template,
        state,
        size=size,
        size_str=size_str,
        sys_size=sys_size,
        tier=tier,
        bat_qty=bat_qty,
        bat_config=bat_config,
        premium_micro=premium_micro,
        is_three_phase=is_three_phase,
        registry_pkg=registry_pkg,
        selections=selections,
        actor_role=actor_role,
    )
    if premium_micro:
        _premium_accessories(cat, state, blockers, system_type=system_type, is_three_phase=is_three_phase)
    _fixed_items(template, state, sys_size=sys_size, bat_config=bat_config, premium_micro=premium_micro)

    mat_total = state["matTotal"]
    return {
        "lines": state["lines"],
        "totals": {"matTotal": mat_total, "allGst": state["allGst"], "sub": mat_total, "grand": mat_total + state["allGst"]},
        "profile": copy.deepcopy(js_or(profile, {})),  # never the caller's catalog record
        "systemConfig": clean(
            {
                "systemType": system_type,
                "size": size,
                "systemSize": sys_size,
                "futureReady": is_future_ready,
                "tier": tier,
                "phase": "3P" if is_three_phase else "1P",
                "configSource": cat["_packConfigSource"],
                "profileKey": profile_key,
                "batteryIncluded": js_number(bat_qty) > 0,
                "batteryQuantity": bat_qty,
                "batterySource": battery_source,
            }
        ),
        "engineering": {"blockers": blockers, "status": "VALID" if not blockers else "BLOCKED"},
    }


def _variable_slots(cat: dict, template: Any, state: dict, **ctx: Any) -> None:
    slots = jsget(template, "slots")
    for slot in slots if truthy(slots) else []:
        slot_category = jsget(slot, "category")
        q_size = ctx["size_str"] if slot_category == "panel" else ctx["sys_size"]
        qty = _slot_qty(slot, premium_micro=ctx["premium_micro"], bat_config=ctx["bat_config"], q_size=q_size)
        if slot_category == "battery":
            qty = ctx["bat_qty"]
        if slot_category == "mccb_box":
            qty = js_max(1, qty) if js_number(ctx["bat_qty"]) > 0 else 0
        if _is_zero(qty):
            continue
        category = jsget(jsget(cat, "categories"), slot_category)
        if not truthy(category):
            continue
        accept = _phase_filter(slot, ctx["is_three_phase"])
        tier = ctx["tier"]
        items = jsget(category, "items")
        available = [i for i in (items if truthy(items) else []) if includes(jsget(i, "tiers"), tier) and _status_active(i) and accept(i)]
        if not available:
            continue
        default_item, default_method = _default_item(slot, available, ctx)
        sales_swap = _slot_sales_swappable(slot)
        own_alternatives = jsget(slot, "alternatives")
        slot_alternatives = own_alternatives if is_array(own_alternatives) and own_alternatives else js_or(_approved_alternate_ids(ctx["registry_pkg"], slot_category), None)
        is_sales_caller = truthy(ctx["actor_role"]) and held_to_sales_rules(ctx["actor_role"])
        default_id = jsget(default_item, "id")
        selection_id = jsget(ctx["selections"], slot_category)
        if truthy(selection_id):
            if is_sales_caller and not sales_swap and not strict_equal(selection_id, default_id):
                raise BomBuildError(
                    f"Sales may not change {js_str(js_or(jsget(slot, 'label'), slot_category))}; it is fixed by the Project Head for this pack.",
                    "SELECTION_NOT_PERMITTED",
                    {"category": slot_category},
                )
            selected = next((i for i in available if strict_equal(jsget(i, "id"), selection_id)), None)
            if selected is None:
                raise _selection_error(category, selection_id, slot_category, tier, accept, ctx["is_three_phase"])
            if is_sales_caller and truthy(slot_alternatives) and not includes(slot_alternatives, selection_id) and not strict_equal(selection_id, default_id):
                raise BomBuildError(
                    f'Component "{js_str(js_or(jsget(selected, "name"), selection_id))}" is not an approved alternative for {js_str(slot_category)}. '
                    "Sales may only select from the approved list for this pack.",
                    "SELECTION_NOT_APPROVED",
                    {"category": slot_category, "approved": copy.deepcopy(slot_alternatives)},
                )
            selection_method = js_or(default_method, "PACKAGE_DEFAULT") if strict_equal(jsget(selected, "id"), default_id) else "SALES_SELECTION"
        else:
            selected = default_item
            selection_method = js_or(default_method, "PACKAGE_DEFAULT")
        if selected is None:
            continue
        _add_variable_line(cat, slot, category, selected, default_item, default_method, qty, state, sales_swap, selection_method, slot_alternatives, ctx)


def _default_item(slot: Any, available: list, ctx: dict) -> tuple[Any, Any]:
    slot_category = jsget(slot, "category")
    def_size = ctx["size_str"] if slot_category == "panel" else ctx["sys_size"]
    ph_default_id = _ph_default_for(slot, ctx["tier"], def_size)
    components = jsget(ctx["registry_pkg"], "components")
    registry_rec = next((c for c in (components if truthy(components) else []) if strict_equal(jsget(c, "role"), slot_category)), None)
    registry_cid = js_or(jsget(registry_rec, "componentId"), jsget(registry_rec, "pinnedComponentId"), None)

    def find(component_id: Any) -> Any:
        return next((i for i in available if strict_equal(jsget(i, "id"), component_id)), None)

    if truthy(ph_default_id) and find(ph_default_id) is not None:
        return find(ph_default_id), "PACK_CONFIG_DEFAULT"
    if truthy(registry_cid) and find(registry_cid) is not None:
        return find(registry_cid), "PACKAGE_REGISTRY_AUTHORITATIVE" if jsget(registry_rec, "derivedBy") == "explicit" else "PACKAGE_REGISTRY_FALLBACK"
    if slot_category == "inverter" and any(truthy(jsget(i, "kw")) for i in available):
        return _nearest_kw(available, js_or(parse_float(ctx["sys_size"]), 3)), "NEAREST_KW_TO_SYSTEM_SIZE"
    return available[0], "LEGACY_FIRST_MATCH_NOT_AUTHORITATIVE"


def _add_variable_line(cat, slot, category, selected, default_item, default_method, qty, state, sales_swap, selection_method, slot_alternatives, ctx) -> None:
    slot_category = jsget(slot, "category")
    size_watts = parse_float(ctx["size"]) * 1000
    if slot_category == "panel" and truthy(jsget(selected, "watt")):
        qty = js_ceil(js_div(size_watts, selected["watt"]))
        state["numPanels"] = qty
    unit_price = js_or(jsget(selected, "price"), 0)
    amount = js_number(qty) * js_number(unit_price)
    gst = js_or(jsget(slot, "gst"), jsget(category, "gstDefault"), 18)
    gst_amt = js_round(amount * js_number(gst) / 100)
    state["matTotal"] += amount
    state["allGst"] += gst_amt
    default_watt = jsget(default_item, "watt")
    state["lines"].append(
        clean(
            {
                "pos": jsget(slot, "pos"),
                "category": slot_category,
                "label": jsget(slot, "label"),
                "name": jsget(selected, "name"),
                "catalogItemId": jsget(selected, "id"),
                "brand": js_or(jsget(selected, "brand"), None),
                "watt": js_or(jsget(selected, "watt"), None),
                "kw": js_or(jsget(selected, "kw"), None),
                "type": js_or(jsget(selected, "type"), None),
                "qty": qty,
                "unitPrice": unit_price,
                "amount": amount,
                "gst": gst,
                "gstAmt": gst_amt,
                "isVariable": True,
                "salesSwap": sales_swap,
                "componentId": jsget(selected, "id"),
                "selectionMethod": selection_method,
                "defaultComponentId": nullish(jsget(default_item, "id"), None),
                "defaultName": nullish(jsget(default_item, "name"), None),
                "defaultUnitPrice": nullish(jsget(default_item, "price"), 0),
                "defaultQty": js_ceil(js_div(size_watts, default_watt)) if slot_category == "panel" and truthy(default_watt) else qty,
                "defaultMethod": default_method,
                "alternatives": copy.deepcopy(slot_alternatives),  # never the caller's pack-config / registry list
            }
        )
    )


def _premium_accessories(cat: dict, state: dict, blockers: list, *, system_type: Any, is_three_phase: bool) -> None:
    is_premium_hybrid = system_type == "hybrid"
    categories = jsget(cat, "categories")
    enphase = jsget(categories, "enphase")
    if truthy(enphase):
        items = jsget(enphase, "items")
        items = items if truthy(items) else []

        def by_role(role: str) -> Any:
            return next((i for i in items if jsget(i, "microAccessoryRole") == role), None)

        candidates = [
            {
                "componentId": jsget(i, "id"),
                "deviceType": i["deviceType"],
                "panelsPerDevice": i["panelsPerDevice"],
                "name": js_or(jsget(i, "name"), None),
                "price": js_or(jsget(i, "price"), None),
                "approvalStatus": js_or(jsget(i, "approvalStatus"), None),
            }
            for i in items
            if jsget(i, "deviceType") == "microinverter" and is_integer(jsget(i, "panelsPerDevice")) and i["panelsPerDevice"] > 0
        ]
        gst_default = jsget(enphase, "gstDefault")

        def add_micro(item: Any, qty: Any, label: str, method: str) -> None:
            if item is None or _is_zero(qty):
                return
            gst = js_or(jsget(item, "gstOverride"), gst_default, 18)
            price = js_or(jsget(item, "price"), 0)
            amount = js_number(qty) * js_number(price)
            gst_amt = js_round(amount * js_number(gst) / 100)
            state["matTotal"] += amount
            state["allGst"] += gst_amt
            state["lines"].append(
                clean(
                    {
                        "pos": len(state["lines"]),
                        "category": "enphase",
                        "label": label,
                        "name": jsget(item, "name"),
                        "catalogItemId": jsget(item, "id"),
                        "qty": qty,
                        "unitPrice": price,
                        "amount": amount,
                        "gst": gst,
                        "gstAmt": gst_amt,
                        "isVariable": False,
                        "componentId": jsget(item, "id"),
                        "selectionMethod": js_or(method, "PACKAGE_DEFAULT"),
                    }
                )
            )

        num_panels = state["numPanels"]
        allocation = allocate_devices(panels=num_panels, candidates=candidates)
        if allocation["status"] == "BLOCKED":
            blockers.append({"code": "ENG-DEV-001", "category": "enphase", "message": allocation["reason"], "panels": num_panels, "unallocated": allocation["unallocatedPanels"]})
        elif allocation["status"] == "VALID":
            for line in allocation["allocations"]:
                item = next((x for x in items if strict_equal(jsget(x, "id"), line["componentId"])), None)
                if item is None:
                    continue
                label = f"{js_str(jsget(item, 'name'))} ({js_str(line['quantity'])} × {js_str(line['panelsPerDevice'])}-panel)"
                add_micro(item, line["quantity"], label, "DEVICE_ALLOCATION_STRUCTURED")
        add_micro(by_role("cable_per_panel"), num_panels, "Q Cable Portrait", "ACCESSORY_ROLE_LOOKUP")
        add_micro(by_role("gateway"), 1, "Envoy S Metered", "ACCESSORY_ROLE_LOOKUP")
        add_micro(by_role("relay"), 1, "Q Relay", "ACCESSORY_ROLE_LOOKUP")
        add_micro(by_role("terminal"), 3 if is_three_phase else 1, "Q Terminal", "ACCESSORY_ROLE_LOOKUP")
        if is_three_phase:
            add_micro(by_role("ct"), 4, "CT 200", "ACCESSORY_ROLE_LOOKUP")
        if is_premium_hybrid:
            add_micro(by_role("system_controller"), 1, "System Controller", "ACCESSORY_ROLE_LOOKUP")
    ug = jsget(categories, "ug_cable")
    if is_premium_hybrid and truthy(ug):
        ug_items = jsget(ug, "items")
        role = "cable_4core" if is_three_phase else "cable_2core"
        cable = next((i for i in (ug_items if truthy(ug_items) else []) if jsget(i, "microAccessoryRole") == role), None)
        if cable is not None:
            price = js_or(jsget(cable, "price"), 0)
            amount = 40 * js_number(price)
            gst_amt = js_round(amount * 18 / 100)
            state["matTotal"] += amount
            state["allGst"] += gst_amt
            state["lines"].append(
                clean(
                    {
                        "pos": len(state["lines"]),
                        "category": "ug_cable",
                        "label": "UG Cable",
                        "name": jsget(cable, "name"),
                        "catalogItemId": jsget(cable, "id"),
                        "qty": 40,
                        "unitPrice": price,
                        "amount": amount,
                        "gst": 18,
                        "gstAmt": gst_amt,
                        "isVariable": False,
                        "componentId": jsget(cable, "id"),
                        "selectionMethod": "PACKAGE_DEFAULT",
                    }
                )
            )


def _fixed_items(template: Any, state: dict, *, sys_size: str, bat_config: str, premium_micro: bool) -> None:
    fixed_items = jsget(template, "fixedItems")
    for item in fixed_items if truthy(fixed_items) else []:
        if premium_micro and truthy(jsget(item, "premiumQty")):
            qty = js_or(jsget(item["premiumQty"], sys_size), 0)
        elif truthy(jsget(item, "batQty")):
            qty = _qty_from(item["batQty"], bat_config, sys_size)
        elif truthy(jsget(item, "qty")):
            qty = js_or(jsget(item["qty"], sys_size), 0)
        else:
            qty = 0
        if _is_zero(qty):
            continue
        price = js_or(jsget(item, "price"), 0)
        amount = js_number(qty) * js_number(price)
        gst = js_or(jsget(item, "gst"), 18)
        gst_amt = js_round(amount * js_number(gst) / 100)
        state["matTotal"] += amount
        state["allGst"] += gst_amt
        state["lines"].append(
            clean(
                {
                    "pos": len(state["lines"]),
                    "category": "fixed",
                    "label": jsget(item, "name"),
                    "name": jsget(item, "name"),
                    "catalogItemId": js_or(jsget(item, "id"), None),
                    "qty": qty,
                    "unitPrice": price,
                    "amount": amount,
                    "gst": gst,
                    "gstAmt": gst_amt,
                    "isVariable": False,
                    "componentId": js_or(jsget(item, "id"), None),
                    "selectionMethod": "FIXED_TEMPLATE",
                }
            )
        )


def build_all_tier_boms(config: Mapping, *, catalog: Mapping, registry: Any = None, pack_config: PackConfigSource = None) -> dict:
    """``buildAllTierBoms``: base, value and premium with per-tier ``tierSelections``."""
    tier_selections = js_or(jsget(config, "tierSelections"), {})
    result = {}
    for tier in ("base", "value", "premium"):
        per_tier = {**config, "tier": tier, "selections": js_or(jsget(tier_selections, tier), {}), "actorRole": jsget(config, "actorRole")}
        result[tier] = build_bom(clean(per_tier), catalog=catalog, registry=registry, pack_config=pack_config)
    return result


def get_alternatives(config: Mapping, *, catalog: Mapping, registry: Any = None, pack_config: PackConfigSource = None) -> dict:
    """``getAlternatives``: per swappable slot, the approved, priced alternatives and the default pick.

    An unknown or missing ``actorRole`` is treated as Sales (least privilege): only Sales-swappable slots, no prices.
    """
    source = js_or(jsget(config, "configSource"), "approved")
    cat = load_catalog(catalog, pack_config, source)
    system_type = jsget(config, "systemType")
    size = jsget(config, "size")
    tier = jsget(config, "tier")
    actor_role = jsget(config, "actorRole")
    future = jsget(config, "futureSystemSize")
    sys_size = js_str(future) if truthy(future) else js_str(size)
    template = jsget(jsget(cat, "bomTemplates"), system_type)
    if not truthy(template):
        raise BomBuildError(f'No BOM template for system type "{js_str(system_type)}"')
    profile = js_or(jsget(jsget(cat, "packageProfiles"), get_profile_key(system_type, tier)), None)
    is_three_phase = jsget(config, "phase") == "3P" or includes(jsget(template, "threePhase"), sys_size)
    bat_qty = _resolve_battery_quantity(system_type, _project_battery_quantity(jsget(config, "batteryQuantity")), profile)
    phase = "3P" if is_three_phase else "1P"
    is_sales = not truthy(actor_role) or held_to_sales_rules(actor_role)
    registry_pkg = _find_registry_package(registry, system_type=system_type, size=sys_size, tier=tier, phase=phase)
    wanted = [jsget(config, "category")] if truthy(jsget(config, "category")) else None
    alternatives: dict = {}
    default_selections: dict = {}
    slots_out: list = []
    ctx = {"tier": tier, "size_str": js_str(size), "sys_size": sys_size, "registry_pkg": registry_pkg}
    slots = jsget(template, "slots")
    for slot in slots if truthy(slots) else []:
        slot_category = jsget(slot, "category")
        if wanted is not None and not includes(wanted, slot_category):
            continue
        if is_sales and not _slot_sales_swappable(slot):
            continue
        if slot_category in ("battery", "mccb_box") and _is_zero(bat_qty):
            continue
        category = jsget(jsget(cat, "categories"), slot_category)
        if not truthy(category):
            continue
        accept = _phase_filter(slot, is_three_phase)
        items = jsget(category, "items")

        def eligible(item: Any) -> bool:
            price = js_number(jsget(item, "price"))
            priced = is_number(price) and price == price and abs(price) != float("inf") and price > 0
            status_ok = _status_active(item) and js_or(jsget(item, "approvalStatus"), "APPROVED") == "APPROVED" and priced
            return includes(jsget(item, "tiers"), tier) and status_ok and accept(item)

        available = [i for i in (items if truthy(items) else []) if eligible(i)]
        if not available:
            continue
        default_item = _alternatives_default(slot, available, ctx)
        own = jsget(slot, "alternatives")
        listed = own if is_array(own) and own else js_or(_approved_alternate_ids(registry_pkg, slot_category), None)
        default_id = jsget(default_item, "id")
        if truthy(listed):
            available = [i for i in available if includes(listed, jsget(i, "id")) or strict_equal(jsget(i, "id"), default_id)]
        entries = []
        for item in available:
            entry = {
                "id": jsget(item, "id"),
                "name": jsget(item, "name"),
                "brand": js_or(jsget(item, "brand"), None),
                "watt": js_or(jsget(item, "watt"), None),
                "kw": js_or(jsget(item, "kw"), None),
                "type": js_or(jsget(item, "type"), None),
                "phase": js_or(jsget(item, "phase"), None),
                "warranty": nullish(jsget(item, "warrantyYears"), jsget(item, "warranty"), None),
                "dcr": js_or(jsget(item, "dcr"), None),
                "isDefault": strict_equal(jsget(item, "id"), default_id),
            }
            if not is_sales:
                entry["price"] = nullish(jsget(item, "price"), None)
                entry["panelType"] = js_or(jsget(item, "panelType"), None)
            entries.append(clean(entry))
        alternatives[slot_category] = entries
        default_selections[slot_category] = default_id
        slots_out.append(clean({"category": slot_category, "label": js_or(jsget(slot, "label"), slot_category), "salesSwap": _slot_sales_swappable(slot), "defaultId": default_id}))
    return {
        "alternatives": alternatives,
        "defaultSelections": default_selections,
        "slots": slots_out,
        "systemConfig": clean(
            {
                "systemType": system_type,
                "size": size,
                "systemSize": sys_size,
                "tier": tier,
                "phase": phase,
                "batteryQuantity": bat_qty,
                "configSource": cat["_packConfigSource"],
            }
        ),
    }


def _alternatives_default(slot: Any, available: list, ctx: dict) -> Any:
    slot_category = jsget(slot, "category")
    def_size = ctx["size_str"] if slot_category == "panel" else ctx["sys_size"]
    ph_default_id = _ph_default_for(slot, ctx["tier"], def_size)
    components = jsget(ctx["registry_pkg"], "components")
    registry_rec = next((c for c in (components if truthy(components) else []) if strict_equal(jsget(c, "role"), slot_category)), None)
    registry_cid = js_or(jsget(registry_rec, "componentId"), jsget(registry_rec, "pinnedComponentId"), None)
    default_item = None
    if truthy(ph_default_id):
        default_item = next((i for i in available if strict_equal(jsget(i, "id"), ph_default_id)), None)
    if default_item is None and truthy(registry_cid):
        default_item = next((i for i in available if strict_equal(jsget(i, "id"), registry_cid)), None)
    if default_item is None and slot_category == "inverter" and any(truthy(jsget(i, "kw")) for i in available):
        default_item = _nearest_kw(available, js_or(parse_float(ctx["sys_size"]), 0))
    return default_item if default_item is not None else available[0]


__all__ = [
    "PACK_CONFIG_SECTIONS",
    "SALES_SELECTABLE_CATEGORIES",
    "BomBuildError",
    "build_all_tier_boms",
    "build_bom",
    "get_alternatives",
    "get_profile_key",
    "is_sales_role",
    "held_to_sales_rules",
    "load_catalog",
    "strip_cost_fields_for_sales",
]
