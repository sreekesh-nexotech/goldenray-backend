"""Pack configuration: schema validation and the DRAFT → SUBMITTED → APPROVED lifecycle.

Port of Flarize ``src/lib/packConfig.js`` (schema ``flarize.pack-config/1``). The store holds two copies of the
Project Head's configuration — ``draft`` (being edited) and ``approved`` (what Sales and pricing use) — plus an
append-only ``history``. Every lifecycle function returns a NEW store; the input is never mutated.

Config sections: ``bomTemplates``, ``structureTemplates``, ``tubeWeights``, ``costs``, ``installationMatrix``,
``transportConfig``, ``marketRates``, ``gst``, ``pricing``, ``futureUpgrade``. :func:`validate_config` is the
``engines.pack_config`` schema check the ``packs_config_version.config`` column relies on (PLAN §2.5).

The package-registry lifecycle (``packageApproval`` / ``packageAuthority`` / ``packageProjection``) lives in
:mod:`engines.package_registry` and is re-exported here.
"""

from __future__ import annotations

import copy
from typing import Any

from engines._jscompat import JsError, clean, is_array, is_nullish, is_num, is_object, js_entries, js_str, js_values, jsget, json_equal, nullish, truthy
from engines.flarize_rbac import CAPABILITY, Authorize, RbacError, actor_label, assert_can
from engines.package_registry import (  # noqa: F401  (re-exported: PLAN names the package lifecycle under pack_config)
    APPROVAL_ERROR,
    PACKAGE_ROLES,
    PACKAGE_STATE,
    PACKAGE_STATUS,
    SALES_SELECTABLE_ROLES,
    ApprovalError,
    approval_summary,
    approve_package,
    archive_package,
    assert_package_selectable_by_actor,
    bulk_approve_packages,
    combo_key_for,
    create_package,
    create_revision,
    derive_package_components,
    duplicate_package,
    edit_package,
    hydrate_registry,
    is_approved_selection,
    is_sales_editable,
    list_packages_for_role,
    reject_package,
    reset_package,
    resolve_package_architecture,
    run_package_checker,
    submit_package,
    visible_for_runtime,
)

PACK_CONFIG_SCHEMA = "flarize.pack-config/1"
DRAFT_STATUS = {"DRAFT": "DRAFT", "SUBMITTED": "SUBMITTED"}
PRICING_MODE = {"PACK_MARKET_RATE": "PACK_MARKET_RATE", "GROSS_MARGIN": "GROSS_MARGIN"}
ROOF_TYPES = ("FLAT", "SHEET", "ELEVATED")
ROOF_TO_STRUCTURE_KEY = {"FLAT": "flatRoof", "SHEET": "sheetRoof", "ELEVATED": "elevated"}
CONFIG_SECTIONS = (
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
PACK_CONFIG_ERROR = {
    "INVALID_SECTION": "INVALID_SECTION",
    "INVALID_VALUE": "INVALID_VALUE",
    "NOT_SUBMITTED": "NOT_SUBMITTED",
    "ALREADY_SUBMITTED": "ALREADY_SUBMITTED",
    "MISSING_TIMESTAMP": "MISSING_TIMESTAMP",
    "NO_CHANGES": "NO_CHANGES",
}
DEFAULT_SALES_SWAP = ("panel", "inverter", "dcdb", "acdb")

_FUTURE_PAIRS = [
    {"systemType": "ongrid", "panelSize": "3", "systemSize": "5sp"},
    {"systemType": "ongrid", "panelSize": "5tp", "systemSize": "6"},
    {"systemType": "ongrid", "panelSize": "5tp", "systemSize": "8"},
    {"systemType": "ongrid", "panelSize": "6", "systemSize": "8"},
    {"systemType": "ongrid", "panelSize": "6", "systemSize": "10"},
    {"systemType": "ongrid", "panelSize": "8", "systemSize": "10"},
    {"systemType": "hybrid", "panelSize": "3", "systemSize": "5"},
    {"systemType": "hybrid", "panelSize": "8", "systemSize": "10"},
]


class PackConfigError(JsError):
    js_name = "PackConfigError"


def _clone(value: Any) -> Any:
    return copy.deepcopy(None if value is None else value)


def _require_at(at: Any) -> Any:
    if not truthy(at):
        raise PackConfigError("Timestamp (at) is required.", PACK_CONFIG_ERROR["MISSING_TIMESTAMP"])
    return at


def _can_edit(actor: Any, authorize: Authorize) -> None:
    try:
        authorize(actor, CAPABILITY["PACKAGE_EDIT"])
        return
    except RbacError:
        pass
    authorize(actor, CAPABILITY["COMMERCIAL_CONFIG_EDIT"])


# ---------------------------------------------------------------------------------------------------------------
# Seeding and accessors
# ---------------------------------------------------------------------------------------------------------------


def seed_from_catalog(catalog: Any, *, at: Any, by: str = "SEED_FROM_CATALOG") -> dict:
    """The initial store built from the catalog the owner maintains: approved v1 and draft v2 (identical)."""
    _require_at(at)
    catalog = catalog if isinstance(catalog, dict) else {}
    templates = _clone(catalog.get("bomTemplates") if truthy(catalog.get("bomTemplates")) else {})
    for template in js_values(templates):
        for slot in (template.get("slots") if isinstance(template, dict) and truthy(template.get("slots")) else []) or []:
            if slot.get("salesSwap") is None:
                slot["salesSwap"] = slot.get("category") in DEFAULT_SALES_SWAP
            if not is_array(slot.get("alternatives")):
                slot["alternatives"] = []
            if not truthy(slot.get("defaults")) or not is_object(slot.get("defaults")):
                slot["defaults"] = {}
    transport = catalog.get("transportConfig") if isinstance(catalog.get("transportConfig"), dict) else {}
    costs = catalog.get("costs") if isinstance(catalog.get("costs"), dict) else {}
    cost_per_km = nullish(transport.get("costPerKm"), 35)
    config = {
        "bomTemplates": templates,
        "structureTemplates": _clone(_or_empty(catalog.get("structureTemplates"))),
        "tubeWeights": _clone(_or_empty(catalog.get("tubeWeights"))),
        "costs": _clone(_or_empty(catalog.get("costs"))),
        "installationMatrix": _clone(_or_empty(catalog.get("installationMatrix"))),
        "transportConfig": {
            "baseDistanceKm": nullish(transport.get("baseDistanceKm"), costs.get("defaultDistKm"), 100),
            "costPerKm": cost_per_km,
            "vehicles": (_clone(transport["vehicles"]) if is_array(transport.get("vehicles")) else [{"vehicleType": "ACE", "vehicleName": "Tata Ace", "ratePerKm": cost_per_km}]),
        },
        "marketRates": _clone(_or_empty(catalog.get("marketRates"))),
        "gst": {"regime": "SOLAR_70_30_COMPOSITE", "ratePct": 8.9, "note": "70% supply @5% + 30% service @18% — decision D2 (2026-09-08)."},
        "pricing": {
            "mode": PRICING_MODE["PACK_MARKET_RATE"],
            "marginPct": 20,
            "note": "Customer price = pack market rate + swap delta + roof add-on; transport beyond base km is a separate customer extra (D3/D4/D6).",
        },
        "futureUpgrade": {
            "pairs": _clone(_FUTURE_PAIRS),
            "note": (
                "Panels at panelSize, inverter/structure/DB/cables/meter at systemSize (owner Q7). PH edits pairs; "
                "market rate key = <systemType>_<tier>[_<bat>]_up<systemSize>. Seeded pairs are proposals — PH confirms."
            ),
        },
    }
    return {
        "schema": PACK_CONFIG_SCHEMA,
        "approved": {"version": 1, "approvedBy": by, "approvedAt": at, "note": "Seeded from data/catalog.json (owner BOM tool v1 data).", "config": _clone(config)},
        "draft": {
            "version": 2,
            "status": DRAFT_STATUS["DRAFT"],
            "basedOn": 1,
            "updatedBy": None,
            "updatedAt": None,
            "submittedBy": None,
            "submittedAt": None,
            "config": _clone(config),
            "changeLog": [],
        },
        "history": [{"version": 1, "approvedBy": by, "approvedAt": at, "note": "Seeded from catalog."}],
    }


def _or_empty(value: Any) -> Any:
    return value if truthy(value) else {}


def approved_config(store: Any) -> Any:
    """The configuration Sales and pricing must use."""
    return nullish(jsget(jsget(store, "approved"), "config"), None)


def draft_config(store: Any) -> Any:
    """The Project Head's working copy."""
    return nullish(jsget(jsget(store, "draft"), "config"), None)


# ---------------------------------------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------------------------------------


def _bad(message: str, detail: Any = None) -> None:
    raise PackConfigError(message, PACK_CONFIG_ERROR["INVALID_VALUE"], detail)


def validate_section(section: Any, value: Any) -> None:
    """Validate one config section; raise :class:`PackConfigError` (``INVALID_SECTION`` / ``INVALID_VALUE``)."""
    if section not in CONFIG_SECTIONS:
        raise PackConfigError(f'Unknown config section "{js_str(section)}".', PACK_CONFIG_ERROR["INVALID_SECTION"], {"section": section, "allowed": list(CONFIG_SECTIONS)})
    if not is_object(value):
        _bad(f"{section} must be an object.")
    if section == "installationMatrix":
        for size, roofs in js_entries(value):
            if not truthy(roofs) or not is_object(roofs):
                _bad(f"installationMatrix.{size} must map roof → amount.")
            for roof, amount in js_entries(roofs):
                if not is_num(amount) or amount < 0:
                    _bad(f"installationMatrix.{size}.{roof} must be a number ≥ 0.", {"size": size, "roof": roof, "amt": amount})
    if section == "marketRates":
        for key, sizes in js_entries(value):
            if not truthy(sizes) or not is_object(sizes):
                _bad(f"marketRates.{key} must map size → amount.")
            for size, amount in js_entries(sizes):
                if not is_num(amount) or amount < 0:
                    _bad(f"marketRates.{key}.{size} must be a number ≥ 0.", {"key": key, "size": size, "amt": amount})
    if section == "transportConfig":
        base = jsget(value, "baseDistanceKm")
        if not is_num(base) or base < 0:
            _bad("transportConfig.baseDistanceKm must be a number ≥ 0.")
        vehicles = jsget(value, "vehicles")
        if not is_array(vehicles) or not vehicles:
            _bad("transportConfig.vehicles must list at least one vehicle.")
        for vehicle in vehicles:
            rate = jsget(vehicle, "ratePerKm")
            if not truthy(jsget(vehicle, "vehicleType")) or not is_num(rate) or rate < 0:
                _bad("Each vehicle needs vehicleType and ratePerKm ≥ 0.", nullish(vehicle, None))
    if section == "costs":
        for key, item in js_entries(value):
            if item is not None and not isinstance(item, str) and (not is_num(item) or item < 0):
                _bad(f"costs.{key} must be a number ≥ 0.", {"k": key, "v": item})
    if section == "gst":
        rate = jsget(value, "ratePct")
        if not is_num(rate) or rate < 0 or rate > 100:
            _bad("gst.ratePct must be 0–100.")
    if section == "pricing":
        if jsget(value, "mode") not in PRICING_MODE.values():
            _bad(f"pricing.mode must be one of {', '.join(PRICING_MODE.values())}.")
    if section == "bomTemplates":
        _validate_templates(value)
    if section == "structureTemplates":
        for key, template in js_entries(value):
            items = jsget(template, "items")
            if not is_array(items):
                _bad(f"structureTemplates.{key}.items must be an array.")
            for item in items:
                if not truthy(jsget(item, "name")) or not truthy(jsget(item, "type")):
                    _bad(f"structureTemplates.{key}: item needs name + type.", item)
                if jsget(item, "type") == "tube" and not is_num(jsget(item, "weightKg")):
                    _bad(f"structureTemplates.{key}.{js_str(jsget(item, 'name'))}: tube needs weightKg.")


def _validate_templates(value: Any) -> None:
    for system, template in js_entries(value):
        sizes = jsget(template, "sizes")
        if system != "upgrade" and (not truthy(sizes) or not is_object(sizes)):
            _bad(f"bomTemplates.{system}.sizes required.")
        slots = jsget(template, "slots")
        if not is_array(slots):
            _bad(f"bomTemplates.{system}.slots must be an array.")
        for slot in slots:
            if not truthy(jsget(slot, "category")):
                _bad(f"bomTemplates.{system}: every slot needs a category.", slot)
            qty = jsget(slot, "qty")
            for quantity in js_values(qty if truthy(qty) else {}):
                if not is_num(quantity) or quantity < 0:
                    _bad(f"bomTemplates.{system}.{js_str(jsget(slot, 'category'))}: qty must be numbers ≥ 0.")
        fixed_items = jsget(template, "fixedItems")
        for item in fixed_items if truthy(fixed_items) else []:
            if not truthy(jsget(item, "name")):
                _bad(f"bomTemplates.{system}: fixed item needs a name.", item)
            price = jsget(item, "price")
            if not is_nullish(price) and (not is_num(price) or price < 0):
                _bad(f"fixed item {js_str(jsget(item, 'name'))}: price ≥ 0.")


def validate_config(config: Any) -> None:
    """Validate every section present in a whole configuration (the ``packs_config_version.config`` schema)."""
    if not isinstance(config, dict):
        _bad("config must be an object.")
    for section in CONFIG_SECTIONS:
        if section in config:
            validate_section(section, config[section])


class schema:  # noqa: N801 — PLAN §2.5 names the config validator ``engines.pack_config.schema``
    """``engines.pack_config.schema.validate(config)`` — the ``packs_config_version.config`` check (PLAN §2.5)."""

    NAME = PACK_CONFIG_SCHEMA
    SECTIONS = CONFIG_SECTIONS
    validate = staticmethod(validate_config)
    validate_section = staticmethod(validate_section)


# ---------------------------------------------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------------------------------------------


def update_draft_section(store: dict, *, actor: Any, section: Any, value: Any, at: Any, note: Any = None, authorize: Authorize = assert_can) -> dict:
    """Replace one section of the DRAFT. Editing a SUBMITTED draft withdraws the submission (back to DRAFT)."""
    _can_edit(actor, authorize)
    _require_at(at)
    validate_section(section, value)
    new = _clone(store)
    draft = new["draft"]
    if draft.get("status") == DRAFT_STATUS["SUBMITTED"]:
        draft["status"] = DRAFT_STATUS["DRAFT"]
    before = jsget(draft["config"], section)
    draft["config"][section] = _clone(value)
    draft["updatedBy"] = actor_label(actor)
    draft["updatedAt"] = at
    draft["changeLog"].append({"at": at, "by": actor_label(actor), "section": section, "note": note, "changed": not json_equal(before, value)})
    return new


def update_draft_template(store: dict, *, actor: Any, system_type: Any, template: Any, at: Any, note: Any = None, authorize: Authorize = assert_can) -> dict:
    """Patch one system type's template (slots / fixedItems / sizes) and replace ``bomTemplates``."""
    current_templates = jsget(draft_config(store), "bomTemplates")
    current = _clone(current_templates if truthy(current_templates) else {})
    key = js_str(system_type)
    existing = current.get(key) if truthy(current.get(key)) else {}
    merged = dict(existing) if isinstance(existing, dict) else {}
    patch = _clone(template)
    if isinstance(patch, dict):
        merged.update(patch)
    current[key] = merged
    return update_draft_section(store, actor=actor, section="bomTemplates", value=current, at=at, note=note if truthy(note) else f"template {key}", authorize=authorize)


def submit_draft(store: dict, *, actor: Any, at: Any, authorize: Authorize = assert_can) -> dict:
    authorize(actor, CAPABILITY["PACKAGE_SUBMIT"])
    _require_at(at)
    new = _clone(store)
    if new["draft"].get("status") == DRAFT_STATUS["SUBMITTED"]:
        raise PackConfigError("Draft is already submitted for approval.", PACK_CONFIG_ERROR["ALREADY_SUBMITTED"])
    if json_equal(new["draft"].get("config"), new["approved"].get("config")):
        raise PackConfigError("Draft is identical to the approved configuration; nothing to submit.", PACK_CONFIG_ERROR["NO_CHANGES"])
    new["draft"]["status"] = DRAFT_STATUS["SUBMITTED"]
    new["draft"]["submittedBy"] = actor_label(actor)
    new["draft"]["submittedAt"] = at
    return new


def approve_draft(store: dict, *, actor: Any, at: Any, note: Any = None, authorize: Authorize = assert_can) -> dict:
    """Admin: promote the SUBMITTED draft to the approved configuration (a new version) and open the next draft."""
    authorize(actor, CAPABILITY["PACKAGE_APPROVE"])
    _require_at(at)
    new = _clone(store)
    draft = new["draft"]
    if draft.get("status") != DRAFT_STATUS["SUBMITTED"]:
        raise PackConfigError("Only a SUBMITTED draft can be approved.", PACK_CONFIG_ERROR["NOT_SUBMITTED"])
    version = draft.get("version")
    by = actor_label(actor)
    new["approved"] = clean(
        {
            "version": version,
            "approvedBy": by,
            "approvedAt": at,
            "note": note,
            "submittedBy": jsget(draft, "submittedBy"),
            "submittedAt": jsget(draft, "submittedAt"),
            "config": _clone(draft.get("config")),
        }
    )
    new["history"].append({"version": version, "approvedBy": by, "approvedAt": at, "note": note, "changes": len(draft.get("changeLog") or [])})
    new["draft"] = {
        "version": version + 1,
        "status": DRAFT_STATUS["DRAFT"],
        "basedOn": version,
        "updatedBy": None,
        "updatedAt": None,
        "submittedBy": None,
        "submittedAt": None,
        "config": _clone(draft.get("config")),
        "changeLog": [],
    }
    return new


def approve_draft_direct(store: dict, *, actor: Any, at: Any, note: Any = None, authorize: Authorize = assert_can) -> dict:
    """Admin one-step approval of the current DRAFT, recorded as submitted AND approved by the Admin."""
    authorize(actor, CAPABILITY["PACKAGE_APPROVE"])
    _require_at(at)
    if store["draft"].get("status") == DRAFT_STATUS["SUBMITTED"]:
        return approve_draft(store, actor=actor, at=at, note=note, authorize=authorize)
    if json_equal(store["draft"].get("config"), store["approved"].get("config")):
        raise PackConfigError("Draft is identical to the approved configuration; nothing to publish.", PACK_CONFIG_ERROR["NO_CHANGES"])
    new = _clone(store)
    by = actor_label(actor)
    new["draft"]["status"] = DRAFT_STATUS["SUBMITTED"]
    new["draft"]["submittedBy"] = by
    new["draft"]["submittedAt"] = at
    new["draft"]["changeLog"].append({"at": at, "by": by, "section": "*", "note": "DIRECT APPROVE by Admin (no Project Head submission)", "changed": False})
    return approve_draft(new, actor=actor, at=at, note=note if truthy(note) else "direct approve by Admin", authorize=authorize)


def reject_draft(store: dict, *, actor: Any, at: Any, reason: Any = None, authorize: Authorize = assert_can) -> dict:
    authorize(actor, CAPABILITY["PACKAGE_APPROVE"])
    _require_at(at)
    new = _clone(store)
    draft = new["draft"]
    if draft.get("status") != DRAFT_STATUS["SUBMITTED"]:
        raise PackConfigError("Only a SUBMITTED draft can be rejected.", PACK_CONFIG_ERROR["NOT_SUBMITTED"])
    by = actor_label(actor)
    draft["status"] = DRAFT_STATUS["DRAFT"]
    draft["rejectedBy"] = by
    draft["rejectedAt"] = at
    draft["rejectionReason"] = reason if truthy(reason) else None
    draft["changeLog"].append({"at": at, "by": by, "section": "*", "note": f"REJECTED: {js_str(reason) if truthy(reason) else ''}", "changed": False})
    return new


def reset_draft(store: dict, *, actor: Any, at: Any, authorize: Authorize = assert_can) -> dict:
    """Discard the draft's edits and start again from the approved configuration."""
    _can_edit(actor, authorize)
    _require_at(at)
    new = _clone(store)
    by = actor_label(actor)
    version = new["approved"].get("version")
    new["draft"] = {
        "version": version + 1,
        "status": DRAFT_STATUS["DRAFT"],
        "basedOn": version,
        "updatedBy": by,
        "updatedAt": at,
        "submittedBy": None,
        "submittedAt": None,
        "config": _clone(new["approved"].get("config")),
        "changeLog": [{"at": at, "by": by, "section": "*", "note": "reset to approved", "changed": False}],
    }
    return new


def describe_store(store: dict) -> dict:
    """UI summary (no mutation): versions, draft status, changed sections, the last 50 log/history entries."""
    changed = [section for section in CONFIG_SECTIONS if not json_equal(jsget(draft_config(store), section), jsget(approved_config(store), section))]
    approved = store["approved"]
    draft = store["draft"]
    return {
        "schema": jsget(store, "schema"),
        "approved": clean(
            {
                "version": jsget(approved, "version"),
                "approvedBy": jsget(approved, "approvedBy"),
                "approvedAt": jsget(approved, "approvedAt"),
                "note": nullish(jsget(approved, "note"), None),
            }
        ),
        "draft": clean(
            {
                "version": jsget(draft, "version"),
                "status": jsget(draft, "status"),
                "basedOn": jsget(draft, "basedOn"),
                "updatedBy": jsget(draft, "updatedBy"),
                "updatedAt": jsget(draft, "updatedAt"),
                "submittedBy": jsget(draft, "submittedBy"),
                "submittedAt": jsget(draft, "submittedAt"),
                "rejectionReason": nullish(jsget(draft, "rejectionReason"), None),
                "changedSections": changed,
                "changeLog": list(draft["changeLog"][-50:]),
            }
        ),
        "history": list(store["history"][-50:]),
    }


# ---------------------------------------------------------------------------------------------------------------
# Pack keys and sizes
# ---------------------------------------------------------------------------------------------------------------


def market_rate_key(*, system_type: Any, tier: Any, battery_config: Any = None, future_system_size: Any = None) -> str:
    """``<systemType>_<tier>[_<bat>][_up<systemSize>]`` exactly as the owner's tool built it."""
    bat = f"_{js_str(nullish(battery_config, 0))}" if system_type == "hybrid" else ""
    up = f"_up{js_str(future_system_size)}" if truthy(future_system_size) else ""
    return f"{js_str(system_type)}_{js_str(tier)}{bat}{up}"


def approved_sizes(config: Any, system_type: Any) -> list[dict]:
    """Sizes a Sales user may offer for a system type, from the APPROVED template: ``[{key, label, phase}]``."""
    template = jsget(jsget(config, "bomTemplates"), system_type)
    if not truthy(template):
        return []
    three_phase = jsget(template, "threePhase")
    three_phase = three_phase if truthy(three_phase) else []
    sizes = jsget(template, "sizes")
    return [{"key": key, "label": label, "phase": "3P" if key in three_phase else "1P"} for key, label in js_entries(sizes if truthy(sizes) else {})]


def future_pairs_for(config: Any, system_type: Any, panel_size: Any) -> list:
    """Future-ready pairs for a panel size: both sizes exist in the template and share a phase."""
    pairs = jsget(jsget(config, "futureUpgrade"), "pairs")
    pairs = pairs if truthy(pairs) else []
    template = jsget(jsget(config, "bomTemplates"), system_type)
    sizes_map = jsget(template, "sizes")
    sizes = {key for key, _ in js_entries(sizes_map if truthy(sizes_map) else {})}
    three_phase = jsget(template, "threePhase")
    three_phase = three_phase if truthy(three_phase) else []

    def phase_of(size: Any) -> str:
        return "3P" if js_str(size) in three_phase else "1P"

    return [
        pair
        for pair in pairs
        if _or_value(jsget(pair, "systemType"), system_type) == system_type
        and js_str(jsget(pair, "panelSize")) == js_str(panel_size)
        and js_str(jsget(pair, "systemSize")) in sizes
        and phase_of(jsget(pair, "panelSize")) == phase_of(jsget(pair, "systemSize"))
    ]


def _or_value(value: Any, fallback: Any) -> Any:
    return value if truthy(value) else fallback
