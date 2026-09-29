"""BOM domain: role taxonomy, component identity, catalogue lifecycle, project BOM, snapshot, lock and status.

Pure ports of the Flarize modules the quotation and project flows build on (engines spec §12, workflows spec C.5,
C.10), each section naming its JavaScript source:

* ``bomRoles.js`` — :class:`Role`, :data:`CATEGORY_TO_ROLE`, :data:`ENPHASE_COMPONENT_ROLES`, :func:`role_for_line`.
* ``componentIdentity.js`` — :func:`to_component`, :func:`find_by_component_id`, :func:`find_duplicate_identities`.
* ``catalogLifecycle.js`` — :func:`classify_catalog_item`, :func:`classify_catalog`, :func:`find_exclusion_gaps`.
* ``projectBom.js`` — :func:`create_project_bom`, :func:`set_component`, :func:`set_quantity`, :func:`remove_role`,
  :func:`effective_lines`, :func:`diff_against_package` (the workspace the lock consumes).
* ``bomSnapshot.js`` — :func:`create_project_bom_snapshot` and the price-immutability helpers.
* ``bomLock.js`` — :func:`attempt_lock` (runs a fresh :func:`engines.engineering_checker.check_project_bom`),
  :func:`lock_summary`.
* ``bomStatus.js`` — :class:`BomStatus`, :func:`quotation_gate`, :func:`can_transition`.

Inputs are JSON-shaped mappings (the legacy objects); results are deep-frozen (:mod:`engines.frozen`) documents
with the JavaScript keys, so a stored snapshot is the legacy snapshot. Nothing reads a clock: every timestamp is the
caller's. A mutation of a LOCKED project BOM raises :class:`BomError`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from engines.frozen import FrozenDict, deep_freeze
from engines.jscompat import UNDEFINED, coalesce, is_nullish, is_number, js_array, js_keys, js_number, js_string, js_trim, js_truthy, prop

__all__ = [
    "Role",
    "NOT_MODELLED_IN_V1",
    "CATEGORY_TO_ROLE",
    "ENPHASE_COMPONENT_ROLES",
    "FIXED_ITEM_NAME_TO_ROLE",
    "PREMIUM_HYBRID_REQUIRED_ROLES",
    "PREMIUM_HYBRID_PROHIBITED",
    "PENDING_CONFIGURATION_CONFIRMATION",
    "role_for_line",
    "ComponentStatus",
    "SELECTABLE_STATUSES",
    "to_component",
    "all_components",
    "find_by_component_id",
    "is_selectable",
    "find_duplicate_identities",
    "CONSUMABLE_CATEGORIES",
    "classify_catalog_item",
    "classify_catalog",
    "find_exclusion_gaps",
    "SelectionMethod",
    "ProjectBomStatus",
    "PROJECT_EDITABLE_ROLES",
    "BomError",
    "is_project_editable_role",
    "create_project_bom",
    "set_component",
    "set_quantity",
    "remove_role",
    "effective_line",
    "effective_lines",
    "diff_against_package",
    "SnapshotStatus",
    "create_project_bom_snapshot",
    "apply_procurement_price_change",
    "price_for_new_quotation",
    "diff_snapshot_against_current_prices",
    "revise_snapshot",
    "LockResult",
    "LockOutcome",
    "warnings_requiring_acknowledgement",
    "requires_acknowledgement",
    "attempt_lock",
    "lock_summary",
    "BomStatus",
    "status_from_validation",
    "can_generate_quotation",
    "can_lock_project_bom",
    "requires_approval",
    "status_reason",
    "can_transition",
    "quotation_gate",
]


# ---------------------------------------------------------------------------------------------------------------------
# bomRoles.js — canonical role taxonomy (decision C32; DC_ISOLATOR deliberately absent, D11-C)
# ---------------------------------------------------------------------------------------------------------------------


class Role(StrEnum):
    PANEL = "PANEL"
    INVERTER = "INVERTER"
    MICRO_INVERTER = "MICRO_INVERTER"
    BATTERY = "BATTERY"
    BATTERY_PROTECTION = "BATTERY_PROTECTION"
    BATTERY_CABLE = "BATTERY_CABLE"
    AC_ISOLATOR = "AC_ISOLATOR"
    ACDB = "ACDB"
    DCDB = "DCDB"
    STRUCTURE = "STRUCTURE"
    CHANGEOVER = "CHANGEOVER"
    MONITORING = "MONITORING"
    PROTECTION = "PROTECTION"
    EARTHING = "EARTHING"
    AC_CABLE = "AC_CABLE"
    DC_CABLE = "DC_CABLE"
    ENERGY_SYSTEM_CONTROLLER = "ENERGY_SYSTEM_CONTROLLER"
    ENPHASE_COMMUNICATION = "ENPHASE_COMMUNICATION"
    METER = "METER"
    OTHER = "OTHER"


#: Decision D11-C: V1 models no separate DC isolator line.
NOT_MODELLED_IN_V1 = ("DC_ISOLATOR",)

#: Engine slot category → role (unambiguous mappings only).
CATEGORY_TO_ROLE: Mapping[str, Role] = FrozenDict(
    panel=Role.PANEL,
    inverter=Role.INVERTER,
    battery=Role.BATTERY,
    mccb_box=Role.BATTERY_PROTECTION,
    battery_cable=Role.BATTERY_CABLE,
    isolator=Role.AC_ISOLATOR,
    acdb=Role.ACDB,
    dcdb=Role.DCDB,
    change_over=Role.CHANGEOVER,
    meter=Role.METER,
    ac_cable=Role.AC_CABLE,
    dc_cable=Role.DC_CABLE,
    ug_cable=Role.AC_CABLE,
    armoured_cable=Role.AC_CABLE,
    earth_cable=Role.EARTHING,
    cb_rod=Role.EARTHING,
    la_cable=Role.PROTECTION,
    structure_material=Role.STRUCTURE,
)

#: Enphase rows share one ``enphase`` category; their role is mapped by component id, never by display name.
ENPHASE_COMPONENT_ROLES: Mapping[str, Role] = FrozenDict(
    en1=Role.MICRO_INVERTER,
    en2=Role.AC_CABLE,
    en3=Role.MONITORING,
    en4=Role.PROTECTION,
    en5=Role.AC_CABLE,
    en6=Role.MONITORING,
    en7=Role.ENERGY_SYSTEM_CONTROLLER,
    en_sysctrl=Role.ENERGY_SYSTEM_CONTROLLER,
    en_flexbat=Role.BATTERY,
)

#: Uncategorised fixed items whose physical identity is unambiguous; everything else stays OTHER.
FIXED_ITEM_NAME_TO_ROLE: Mapping[str, Role] = FrozenDict({"25mm Battery Cable": Role.BATTERY_CABLE, "40A Change Over Switch": Role.CHANGEOVER})

#: Decision D12: roles a Premium (Enphase) hybrid system requires, and the Deye/SEG parts it prohibits.
PREMIUM_HYBRID_REQUIRED_ROLES = (Role.PANEL, Role.MICRO_INVERTER, Role.BATTERY, Role.ENERGY_SYSTEM_CONTROLLER)
PREMIUM_HYBRID_PROHIBITED: Mapping[str, str] = FrozenDict(
    {
        "mb1": "Generic 160A MCCB is a Deye/SEG DC-coupled device. Prohibited on Enphase architecture (D12).",
        "25mm Battery Cable": "Generic DC battery cable is Deye/SEG-specific. Prohibited on Enphase architecture (D12).",
    }
)

#: Decision D12 (PENDING): Enphase parts whose supplied configuration is unconfirmed.
PENDING_CONFIGURATION_CONFIRMATION: Mapping[str, str] = FrozenDict(
    en3="Envoy S Metered — may be integrated into the IQ System Controller. Supplied configuration unconfirmed.",
    en4="IQ Relay — may be integrated into the IQ System Controller. Supplied configuration unconfirmed.",
    en6="CT 200 — quantity and necessity unconfirmed against the supplied configuration.",
)


def _lookup(table: Mapping[str, Any], key: Any) -> Any:
    """``table[key]`` with JavaScript property-key conversion (a number key is its text)."""
    if is_nullish(key):
        return table.get(js_string(key))
    return table.get(key if isinstance(key, str) else js_string(key))


def role_for_line(line: Any) -> Role:
    """``roleForLine``: Enphase id map, then category map, then ``isStructure``, then the fixed-name map, else OTHER."""
    if not js_truthy(line):
        return Role.OTHER
    component_id = prop(line, "itemId") if js_truthy(prop(line, "itemId")) else prop(line, "catalogItemId")
    if js_truthy(component_id) and _lookup(ENPHASE_COMPONENT_ROLES, component_id):
        return _lookup(ENPHASE_COMPONENT_ROLES, component_id)
    category = prop(line, "category")
    if js_truthy(category) and _lookup(CATEGORY_TO_ROLE, category):
        return _lookup(CATEGORY_TO_ROLE, category)
    if js_truthy(prop(line, "isStructure")):
        return Role.STRUCTURE
    name = prop(line, "name")
    name = js_trim(js_string(name if js_truthy(name) else ""))
    return FIXED_ITEM_NAME_TO_ROLE.get(name, Role.OTHER)


# ---------------------------------------------------------------------------------------------------------------------
# componentIdentity.js — a component is its componentId, never its display name
# ---------------------------------------------------------------------------------------------------------------------


class ComponentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    TEST = "TEST"
    DUPLICATE = "DUPLICATE"
    PENDING_REVIEW = "PENDING_REVIEW"


SELECTABLE_STATUSES = (ComponentStatus.ACTIVE,)

_CABLE_CATEGORY = re.compile(r"cable|conductor")


def _or_null(value: Any) -> Any:
    return value if js_truthy(value) else None


def _build_spec(item: Mapping) -> str | None:
    bits = []
    for key, suffix in (("watt", "W"), ("kw", "kW"), ("kwh", "kWh")):
        if js_truthy(prop(item, key)):
            bits.append(f"{js_string(item[key])}{suffix}")
    if js_truthy(prop(item, "phase")):
        bits.append(js_string(item["phase"]))
    if js_truthy(prop(item, "sqmm")):
        bits.append(f"{js_string(item['sqmm'])}sqmm")
    if js_truthy(prop(item, "panelType")):
        bits.append(js_string(item["panelType"]))
    if js_truthy(prop(item, "mpptCount")):
        bits.append(f"{js_string(item['mpptCount'])} MPPT")
    return " · ".join(bits) or None


def to_component(category_key: Any, item: Any) -> Mapping | None:
    """``toComponent``: the canonical identity shape of a raw catalogue item (``status`` defaults to ACTIVE)."""
    if not js_truthy(item):
        return None
    status = prop(item, "status") if js_truthy(prop(item, "status")) else ComponentStatus.ACTIVE.value
    tiers = js_array(prop(item, "tiers"))
    unit = prop(item, "unit")
    return deep_freeze(
        {
            "componentId": prop(item, "id"),
            "category": category_key,
            "brand": _or_null(prop(item, "brand")),
            "model": _or_null(prop(item, "model")),
            "name": _or_null(prop(item, "name")),
            "specification": _build_spec(item),
            "unit": unit if js_truthy(unit) else ("m" if _CABLE_CATEGORY.search(js_string(category_key)) else "nos"),
            "status": status,
            "active": status == ComponentStatus.ACTIVE.value,
            "tiers": tiers if tiers is not None else [],
            "price": coalesce(prop(item, "price"), None),
            "raw": item,
        }
    )


def _categories(catalog: Any) -> list[tuple[str, Mapping]]:
    categories = prop(catalog, "categories")
    return [(key, categories[key]) for key in js_keys(categories)]


def _items(category: Any) -> list:
    return js_array(prop(category, "items")) or []


def all_components(catalog: Any) -> tuple:
    """``allComponents``: every catalogue item, flattened, identity applied."""
    return tuple(to_component(key, item) for key, category in _categories(catalog) for item in _items(category))


def find_by_component_id(catalog: Any, component_id: Any) -> Mapping | None:
    """``findByComponentId``: the only sanctioned lookup (first category, in catalogue order, that holds the id)."""
    if not js_truthy(component_id):
        return None
    for key, category in _categories(catalog):
        for item in _items(category):
            if prop(item, "id") == component_id:
                return to_component(key, item)
    return None


def is_selectable(component: Any) -> bool:
    """``isSelectable``: ACTIVE and offered in at least one tier."""
    if not js_truthy(component):
        return False
    return prop(component, "status") in SELECTABLE_STATUSES and len(prop(component, "tiers")) > 0


def find_duplicate_identities(catalog: Any) -> tuple:
    """``findDuplicateIdentities``: ids sharing ``category|brand|model-or-name`` (lower-cased, trimmed)."""
    seen: dict[str, list] = {}
    for component in all_components(catalog):
        model = component["model"] if js_truthy(component["model"]) else component["name"]
        key = "|".join(
            [
                js_string(component["category"]),
                js_trim(js_string(component["brand"] if js_truthy(component["brand"]) else "").lower()),
                js_trim(js_string(model if js_truthy(model) else "").lower()),
            ]
        )
        seen.setdefault(key, []).append(component["componentId"])
    return deep_freeze([{"key": key, "componentIds": ids} for key, ids in seen.items() if len(ids) > 1])


# ---------------------------------------------------------------------------------------------------------------------
# catalogLifecycle.js — computed item status (not persisted; drives validation, filters nothing)
# ---------------------------------------------------------------------------------------------------------------------

_SPACE = "[\t\n\v\f\r    -     　﻿]"
_PLACEHOLDER_NAME = re.compile(rf"^{_SPACE}*(new|test|demo|sample|dummy|temp|tmp|xxx|asdf|qwerty)\b", re.IGNORECASE | re.ASCII)
_SLOPPY_NAME = re.compile(rf"^[a-z]+{_SPACE}*\d{{2,4}}{_SPACE}*\Z", re.ASCII)

#: Categories whose items are consumables and legitimately carry no brand.
CONSUMABLE_CATEGORIES = frozenset(
    {
        "mc4_connector",
        "cable_tie",
        "flexible_pipe",
        "ss_screw",
        "copper_lug",
        "insulation_tape",
        "conduit_pipe",
        "solar_clamp",
        "structure_material",
        "painting_material",
        "welding_material",
        "earthing_material",
    }
)


def _price_is_zero(item: Any) -> bool:
    price = prop(item, "price")
    return is_number(price) and price == 0


def classify_catalog_item(category_key: Any, item: Any, context: Mapping | None = None) -> Mapping:
    """``classifyCatalogItem`` → ``{status, reasons, suggestedAction}`` (an explicit valid status wins)."""
    context = context or {}
    name = js_trim(js_string(prop(item, "name") if js_truthy(prop(item, "name")) else ""))
    consumable = category_key in CONSUMABLE_CATEGORIES
    explicit = prop(item, "status")
    if js_truthy(explicit) and explicit in {status.value for status in ComponentStatus}:
        return deep_freeze({"status": explicit, "reasons": ["explicitly set on the record"], "suggestedAction": "none"})

    placeholder = bool(_PLACEHOLDER_NAME.search(name))
    sloppy = bool(_SLOPPY_NAME.search(name))
    no_brand = not consumable and not js_truthy(prop(item, "brand"))
    reasons = []
    if placeholder:
        reasons.append(f'name "{name}" looks like a placeholder')
    if sloppy:
        reasons.append(f'name "{name}" is lowercase and unqualified')
    if no_brand:
        reasons.append("no brand on a branded-category item")
    if _price_is_zero(item):
        reasons.append("price is zero")
    tiers = js_array(prop(item, "tiers"))
    if tiers is not None and len(tiers) == 0:
        reasons.append("no tier assigned")
    duplicate_of = context.get("duplicateOf")
    if js_truthy(duplicate_of):
        reasons.append(f"duplicate identity of {js_string(duplicate_of)}")

    status, action = ComponentStatus.ACTIVE, "none"
    if js_truthy(duplicate_of):
        status, action = ComponentStatus.DUPLICATE, "link to canonical record, then retire"
    elif placeholder and _price_is_zero(item):
        status, action = ComponentStatus.TEST, "remove from selection lists, then delete after review"
    elif placeholder or (_price_is_zero(item) and not consumable):
        status, action = ComponentStatus.PENDING_REVIEW, "complete the record or retire it"
    elif sloppy or no_brand:
        status, action = ComponentStatus.PENDING_REVIEW, "confirm this is a real product and complete brand/model"
    return deep_freeze({"status": status.value, "reasons": reasons, "suggestedAction": action})


def classify_catalog(catalog: Any) -> Mapping:
    """``classifyCatalog``: the read-only report of every item that is not ACTIVE."""
    report, counts, total = [], {}, 0
    for key, category in _categories(catalog):
        for item in _items(category):
            total += 1
            classified = classify_catalog_item(key, item)
            if classified["status"] != ComponentStatus.ACTIVE:
                report.append({"categoryKey": key, "componentId": prop(item, "id"), "name": prop(item, "name"), "price": prop(item, "price"), "tiers": prop(item, "tiers"), **classified})
                counts[classified["status"]] = counts.get(classified["status"], 0) + 1
    return deep_freeze({"total": total, "flagged": len(report), "counts": counts, "items": report})


def find_exclusion_gaps(catalog: Any, tiers: Sequence[str] = ("base", "value", "premium")) -> tuple:
    """``findExclusionGaps``: category × tier pairs that would lose every candidate if non-ACTIVE items were hidden."""
    gaps = []
    for key, category in _categories(catalog):
        for tier in tiers:
            in_tier = [item for item in _items(category) if tier in (js_array(prop(item, "tiers")) or [])]
            if not in_tier:
                continue
            if not any(classify_catalog_item(key, item)["status"] == ComponentStatus.ACTIVE for item in in_tier):
                gaps.append(
                    {
                        "categoryKey": key,
                        "tier": tier,
                        "wouldLose": [f"{js_string(prop(item, 'id'))}:{js_string(prop(item, 'name'))}" for item in in_tier],
                        "impact": f'Excluding non-ACTIVE items leaves tier "{tier}" with no {key} at all.',
                    }
                )
    return deep_freeze(gaps)


# ---------------------------------------------------------------------------------------------------------------------
# projectBom.js — the Project Head's BOM (selection only: no validation, no pricing)
# ---------------------------------------------------------------------------------------------------------------------


class SelectionMethod(StrEnum):
    PACKAGE_DEFAULT = "PACKAGE_DEFAULT"
    PROJECT_HEAD_OVERRIDE = "PROJECT_HEAD_OVERRIDE"
    ENGINEER_PROPOSAL = "ENGINEER_PROPOSAL"
    PROCUREMENT_SUBSTITUTION = "PROCUREMENT_SUBSTITUTION"
    FALLBACK = "FALLBACK"


class ProjectBomStatus(StrEnum):
    DRAFT = "DRAFT"
    LOCKED = "LOCKED"


#: Roles a Project Head may select or change (every role; DC_ISOLATOR is not a role, D11-C).
PROJECT_EDITABLE_ROLES = (
    Role.PANEL,
    Role.INVERTER,
    Role.MICRO_INVERTER,
    Role.BATTERY,
    Role.STRUCTURE,
    Role.ACDB,
    Role.DCDB,
    Role.AC_ISOLATOR,
    Role.CHANGEOVER,
    Role.AC_CABLE,
    Role.DC_CABLE,
    Role.BATTERY_PROTECTION,
    Role.BATTERY_CABLE,
    Role.MONITORING,
    Role.EARTHING,
    Role.PROTECTION,
    Role.ENERGY_SYSTEM_CONTROLLER,
    Role.ENPHASE_COMMUNICATION,
    Role.METER,
    Role.OTHER,
)


class BomError(ValueError):
    """A refused project-BOM operation (the JavaScript ``throw new Error(message)``), with a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_project_editable_role(role: Any) -> bool:
    return role in PROJECT_EDITABLE_ROLES


def create_project_bom(
    *,
    project_id: Any = UNDEFINED,
    package_id: Any = UNDEFINED,
    architecture: Any = None,
    sys_type: Any = None,
    tier: Any = None,
    size_kw: Any = None,
    phase: Any = None,
    lines: Sequence[Mapping] = (),
    created_by: Any = None,
    created_at: Any = None,
) -> Mapping:
    """``createProjectBom``: a DRAFT BOM whose ``packageLines`` are the frozen package default."""
    package_lines = [
        {
            "role": prop(line, "role"),
            "componentId": coalesce(prop(line, "componentId"), None),
            "quantity": coalesce(prop(line, "quantity"), 0),
            "selectionMethod": SelectionMethod.PACKAGE_DEFAULT.value,
            "selectedBy": None,
            "selectionTimestamp": None,
            "reason": None,
        }
        for line in lines
    ]
    return deep_freeze(
        {
            "projectId": project_id,
            "packageId": package_id,
            "architecture": architecture,
            "sysType": sys_type,
            "tier": tier,
            "sizeKw": size_kw,
            "phase": phase,
            "status": ProjectBomStatus.DRAFT.value,
            "createdBy": created_by,
            "createdAt": created_at,
            "packageLines": package_lines,
            "overrides": {},
            "removedRoles": [],
            "history": [],
            "lock": None,
        }
    )


def _assert_editable(bom: Any, what: str) -> None:
    if not js_truthy(bom):
        raise BomError("NO_PROJECT_BOM", "no project BOM")
    if prop(bom, "status") == ProjectBomStatus.LOCKED:
        raise BomError("PROJECT_BOM_LOCKED", f"Project BOM {js_string(prop(bom, 'projectId'))} is LOCKED and cannot be {what}. Revise the snapshot instead.")


def effective_line(bom: Any, role: Any) -> Mapping | None:
    """``effectiveLine``: the project override, else the package default; ``None`` for a removed role."""
    if not js_truthy(bom):
        return None
    if role in bom["removedRoles"]:
        return None
    override = bom["overrides"].get(role) if isinstance(role, str) else None
    if override:
        return override
    return next((line for line in bom["packageLines"] if line["role"] == role), None)


def effective_lines(bom: Mapping) -> tuple:
    """``effectiveLines``: every effective line, ordered by role (never by insertion or UI order)."""
    roles = list(dict.fromkeys([line["role"] for line in bom["packageLines"]] + js_keys(bom["overrides"])))
    ordered = sorted((role for role in roles if role not in bom["removedRoles"]), key=js_string)
    return tuple(line for line in (effective_line(bom, role) for role in ordered) if line)


def set_component(
    bom: Any,
    *,
    role: Any = UNDEFINED,
    component_id: Any = UNDEFINED,
    quantity: Any = UNDEFINED,
    selected_by: Any = None,
    at: Any = None,
    reason: Any = None,
    method: str = SelectionMethod.PROJECT_HEAD_OVERRIDE.value,
) -> Mapping:
    """``setComponent``: records a Project Head selection. No engineering validation by design (the checker decides)."""
    _assert_editable(bom, "edited")
    if not js_truthy(role):
        raise BomError("ROLE_REQUIRED", "role is required")
    if not is_project_editable_role(role):
        raise BomError("ROLE_NOT_EDITABLE", f'role "{js_string(role)}" is not a project-editable role')
    if is_nullish(component_id):
        raise BomError("COMPONENT_REQUIRED", "componentId is required — use removeRole() to remove a line explicitly")
    previous = effective_line(bom, role)
    entry = {
        "role": role,
        "componentId": component_id,
        "quantity": quantity if not is_nullish(quantity) else coalesce(previous["quantity"] if previous else None, 0),
        "selectionMethod": method,
        "selectedBy": selected_by,
        "selectionTimestamp": at,
        "reason": reason,
    }
    history = {"action": "SET_COMPONENT", "role": role, "from": previous["componentId"] if previous else None, "to": component_id, "by": selected_by, "at": at, "reason": reason}
    return deep_freeze(
        {
            **bom,
            "overrides": {**bom["overrides"], role: entry},
            "removedRoles": [removed for removed in bom["removedRoles"] if removed != role],
            "history": [*bom["history"], history],
        }
    )


def set_quantity(bom: Any, *, role: Any, quantity: Any, selected_by: Any = None, at: Any = None) -> Mapping:
    """``setQuantity``: a quantity change only; the identity is untouched."""
    _assert_editable(bom, "edited")
    previous = effective_line(bom, role)
    if not previous:
        raise BomError("ROLE_NOT_PRESENT", f'role "{js_string(role)}" is not present in the BOM')
    return set_component(bom, role=role, component_id=previous["componentId"], quantity=quantity, selected_by=selected_by, at=at, reason="quantity change")


def remove_role(bom: Any, *, role: Any, selected_by: Any = None, at: Any = None, reason: Any = None) -> Mapping:
    """``removeRole``: the only way a line leaves the BOM — an explicit Project Head act."""
    _assert_editable(bom, "edited")
    previous = effective_line(bom, role)
    overrides = {key: value for key, value in bom["overrides"].items() if key != role}
    history = {"action": "REMOVE_ROLE", "role": role, "from": previous["componentId"] if previous else None, "to": None, "by": selected_by, "at": at, "reason": reason}
    return deep_freeze({**bom, "overrides": overrides, "removedRoles": list(dict.fromkeys([*bom["removedRoles"], role])), "history": [*bom["history"], history]})


def diff_against_package(bom: Mapping) -> tuple:
    """``diffAgainstPackage``: the roles the project changed or removed, relative to the package default."""
    out = []
    for role in sorted(js_keys(bom["overrides"]), key=js_string):
        package = next((line for line in bom["packageLines"] if line["role"] == role), None)
        override = bom["overrides"][role]
        if not package or package["componentId"] != override["componentId"] or not _strict_equal(package["quantity"], override["quantity"]):
            out.append(
                {
                    "role": role,
                    "packageComponentId": package["componentId"] if package else None,
                    "projectComponentId": override["componentId"],
                    "packageQuantity": package["quantity"] if package else None,
                    "projectQuantity": override["quantity"],
                    "selectedBy": override["selectedBy"],
                    "at": override["selectionTimestamp"],
                }
            )
    for role in sorted(bom["removedRoles"], key=js_string):
        package = next((line for line in bom["packageLines"] if line["role"] == role), None)
        if package:
            out.append(
                {
                    "role": role,
                    "packageComponentId": package["componentId"],
                    "projectComponentId": None,
                    "packageQuantity": package["quantity"],
                    "projectQuantity": 0,
                    "removed": True,
                }
            )
    return deep_freeze(out)


def _strict_equal(left: Any, right: Any) -> bool:
    """``left === right`` for JSON scalars (numbers by value, no cross-type coercion)."""
    if is_number(left) and is_number(right):
        return left == right
    if is_number(left) or is_number(right):
        return False
    return type(left) is type(right) and left == right


# ---------------------------------------------------------------------------------------------------------------------
# bomSnapshot.js — the frozen project BOM and price immutability (Phase 1D §13)
# ---------------------------------------------------------------------------------------------------------------------


class SnapshotStatus(StrEnum):
    DRAFT = "DRAFT"
    LOCKED = "LOCKED"
    SUPERSEDED = "SUPERSEDED"


def _first_truthy(*values: Any) -> Any:
    for value in values:
        if js_truthy(value):
            return value
    return None


def create_project_bom_snapshot(
    *,
    project_id: Any = UNDEFINED,
    bom_lines: Sequence[Mapping] = (),
    selections: Mapping | None = None,
    locked_by: Any = None,
    locked_at: Any = None,
    data_version: Any = None,
    engineering_status: Any = None,
) -> Mapping:
    """``createProjectBomSnapshot``: every line records identity, provenance and the price at the moment of locking."""
    selections = selections or {}
    lines = []
    for line in bom_lines:
        role = coalesce(prop(line, "role"), None) if js_truthy(prop(line, "role")) else None
        selection = _lookup(selections, role) or {}
        lines.append(
            {
                "componentId": _first_truthy(prop(line, "componentId"), prop(line, "itemId"), prop(line, "catalogItemId")),
                "componentDataVersion": coalesce(prop(line, "componentDataVersion"), data_version),
                "role": role,
                "quantity": coalesce(prop(line, "quantity"), prop(line, "qty"), None),
                "unitPurchaseCost": coalesce(prop(line, "unitPurchaseCost"), prop(line, "unitPrice"), None),
                "unitSellingPrice": coalesce(prop(line, "unitSellingPrice"), None),
                "engineeringApprovalState": coalesce(prop(line, "engineeringApprovalState"), engineering_status),
                "selectionMethod": _first_truthy(prop(selection, "selectionMethod"), prop(line, "selectionMethod")),
                "selectedBy": _first_truthy(prop(selection, "selectedBy"), prop(line, "selectedBy")),
                "selectionTimestamp": _first_truthy(prop(selection, "at"), prop(line, "selectionTimestamp")),
            }
        )
    return deep_freeze(
        {
            "projectId": project_id,
            "status": SnapshotStatus.LOCKED.value,
            "lockedBy": _or_null(locked_by),
            "lockedAt": _or_null(locked_at),
            "dataVersion": data_version,
            "engineeringStatus": engineering_status,
            "lines": lines,
        }
    )


def apply_procurement_price_change(snapshot: Any, price_updates: Mapping | None = None) -> Any:
    """``applyProcurementPriceChange``: a LOCKED snapshot is returned unchanged — the same object."""
    price_updates = price_updates or {}
    if not js_truthy(snapshot) or snapshot["status"] == SnapshotStatus.LOCKED:
        return snapshot
    lines = [{**line, "unitPurchaseCost": _lookup(price_updates, line["componentId"])} if not is_nullish(_lookup(price_updates, line["componentId"])) else line for line in snapshot["lines"]]
    return deep_freeze({**snapshot, "lines": lines})


def price_for_new_quotation(bom_lines: Sequence[Mapping], catalog_prices: Mapping | None = None) -> tuple:
    """``priceForNewQuotation``: a new quotation is priced from the current catalogue, never from a snapshot."""
    catalog_prices = catalog_prices or {}
    out = []
    for line in bom_lines:
        component_id = _first_truthy(prop(line, "componentId"), prop(line, "itemId"))
        out.append({**line, "unitPurchaseCost": coalesce(_lookup(catalog_prices, component_id), prop(line, "unitPurchaseCost"), None)})
    return deep_freeze(out)


def diff_snapshot_against_current_prices(snapshot: Any, catalog_prices: Mapping | None = None) -> tuple:
    """``diffSnapshotAgainstCurrentPrices``: what a price change *would* have done (audit), without doing it."""
    catalog_prices = catalog_prices or {}
    out = []
    for line in prop(snapshot, "lines") or ():
        current = _lookup(catalog_prices, line["componentId"])
        if is_nullish(current) or _strict_equal(current, line["unitPurchaseCost"]):
            continue
        delta = js_number(current) - js_number(line["unitPurchaseCost"])
        out.append({"componentId": line["componentId"], "role": line["role"], "lockedPrice": line["unitPurchaseCost"], "currentPrice": current, "delta": delta if delta.is_finite() else None})
    return deep_freeze(out)


def revise_snapshot(snapshot: Mapping, changes: Mapping | None = None, *, revised_by: Any = None, revised_at: Any = None) -> Mapping:
    """``reviseSnapshot``: a NEW locked snapshot; the original comes back as a SUPERSEDED copy."""
    changes = changes or {}
    current = create_project_bom_snapshot(
        project_id=prop(snapshot, "projectId"),
        bom_lines=changes.get("bomLines") or snapshot["lines"],
        selections=changes.get("selections") or {},
        locked_by=revised_by,
        locked_at=revised_at,
        data_version=coalesce(changes.get("dataVersion", UNDEFINED), prop(snapshot, "dataVersion")),
        engineering_status=coalesce(changes.get("engineeringStatus", UNDEFINED), prop(snapshot, "engineeringStatus")),
    )
    return deep_freeze({"previous": {**snapshot, "status": SnapshotStatus.SUPERSEDED.value}, "current": current})


# ---------------------------------------------------------------------------------------------------------------------
# bomLock.js — the lock decision (a gate, never a repair)
# ---------------------------------------------------------------------------------------------------------------------


class LockResult(StrEnum):
    LOCKED = "LOCKED"
    REJECTED_BLOCKED = "REJECTED_BLOCKED"
    REJECTED_UNACKNOWLEDGED = "REJECTED_UNACKNOWLEDGED"
    ALREADY_LOCKED = "ALREADY_LOCKED"


def warnings_requiring_acknowledgement(rule_set: Any = None) -> tuple[str, ...]:
    """``WARNINGS_REQUIRING_ACKNOWLEDGEMENT``: the rule set's warning rules flagged ``requiresAcknowledgement``."""
    from engines import engineering_checker  # the checker imports this module's role taxonomy

    rules = rule_set or engineering_checker.DEFAULT_RULE_SET
    return tuple(rule.code for rule in rules.rules if rule.requires_acknowledgement)


def requires_acknowledgement(rule_id: str, rule_set: Any = None) -> bool:
    return rule_id in warnings_requiring_acknowledgement(rule_set)


@dataclass(frozen=True)
class LockOutcome:
    """``attemptLock``'s result: ``{result, locked, bom, snapshot, validation, missingAcknowledgements, reason}``."""

    result: LockResult
    locked: bool
    bom: Mapping
    snapshot: Mapping | None
    validation: Any
    missing_acknowledgements: tuple
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.value,
            "locked": self.locked,
            "bom": self.bom,
            "snapshot": self.snapshot,
            "validation": self.validation.as_dict() if self.validation is not None else None,
            "missingAcknowledgements": self.missing_acknowledgements,
            "reason": self.reason,
        }


def attempt_lock(
    *,
    bom: Any,
    catalog: Any = None,
    battery_master: Mapping | None = None,
    catalog_version: Any = None,
    locked_by: Any = None,
    locked_at: Any = None,
    acknowledgements: Sequence[Mapping] = (),
    price_lookup: Mapping | None = None,
    upgrade: Any = None,
    future_upgrade: Any = False,
    rule_set: Any = None,
) -> LockOutcome:
    """``attemptLock``: a complete, fresh checker pass, then LOCKED / REJECTED_BLOCKED / REJECTED_UNACKNOWLEDGED.

    A BLOCKED verdict leaves the BOM untouched (editable); warnings flagged ``requiresAcknowledgement`` in the rule set
    need a named acknowledgement (``{ruleId, by, at, note}``); otherwise the BOM is frozen into a LOCKED snapshot
    carrying the package, architecture, rules version, verdict and acknowledgements.
    """
    from engines import engineering_checker  # the checker imports this module's role taxonomy

    if not js_truthy(bom):
        raise BomError("NO_PROJECT_BOM", "no project BOM")
    price_lookup = price_lookup or {}
    if prop(bom, "status") == ProjectBomStatus.LOCKED:
        lock = prop(bom, "lock")
        return LockOutcome(LockResult.ALREADY_LOCKED, True, bom, lock if js_truthy(lock) else None, None, (), "The project BOM is already locked. Create a revision instead.")

    validation = engineering_checker.check_project_bom(
        bom=bom,
        catalog=catalog,
        battery_master=battery_master or {},
        at=locked_at,
        catalog_version=catalog_version,
        upgrade=upgrade,
        future_upgrade=future_upgrade,
        rule_set=rule_set,
    )
    if validation.status == engineering_checker.CheckStatus.BLOCKED:
        blockers = [finding for finding in validation.findings if finding.severity == engineering_checker.Severity.BLOCK]
        reason = f"Lock rejected: {len(blockers)} safety-critical blocker(s). The BOM has not been modified. Fix the configuration: " + " | ".join(
            f"{finding.rule_id} {finding.message}" for finding in blockers
        )
        return LockOutcome(LockResult.REJECTED_BLOCKED, False, bom, None, validation, (), reason)

    acknowledged = {prop(ack, "ruleId") for ack in acknowledgements}
    needs_ack = warnings_requiring_acknowledgement(rule_set)
    missing = tuple(
        FrozenDict(ruleId=finding.rule_id, message=finding.message)
        for finding in validation.findings
        if finding.severity == engineering_checker.Severity.WARN and finding.rule_id in needs_ack and finding.rule_id not in acknowledged
    )
    if missing:
        reason = f"Lock rejected: {len(missing)} warning(s) require explicit acknowledgement — " + ", ".join(item["ruleId"] for item in missing)
        return LockOutcome(LockResult.REJECTED_UNACKNOWLEDGED, False, bom, None, validation, missing, reason)

    lines = []
    for line in effective_lines(bom):
        prices = _lookup(price_lookup, line["componentId"])
        lines.append(
            {
                "componentId": line["componentId"],
                "role": line["role"],
                "qty": line["quantity"],
                "unitPurchaseCost": coalesce(prop(prices, "purchaseCost"), None),
                "unitSellingPrice": coalesce(prop(prices, "sellingPrice"), None),
                "componentDataVersion": catalog_version,
                "engineeringApprovalState": validation.status.value,
                "selectionMethod": line["selectionMethod"],
                "selectedBy": line["selectedBy"],
                "selectionTimestamp": line["selectionTimestamp"],
            }
        )
    snapshot = create_project_bom_snapshot(
        project_id=prop(bom, "projectId"),
        bom_lines=lines,
        locked_by=locked_by,
        locked_at=locked_at,
        data_version=catalog_version,
        engineering_status=validation.status.value,
    )
    lock_record = deep_freeze(
        {
            **snapshot,
            "packageId": prop(bom, "packageId"),
            "architecture": prop(bom, "architecture"),
            "rulesVersion": validation.rules_version,
            "validationStatus": validation.status.value,
            "acknowledgements": [dict(ack) for ack in acknowledgements],
        }
    )
    locked_bom = deep_freeze({**bom, "status": ProjectBomStatus.LOCKED.value, "lock": lock_record})
    reason = "Locked with acknowledged warnings." if validation.status == engineering_checker.CheckStatus.WARNING else "Locked. Engineering validation passed."
    return LockOutcome(LockResult.LOCKED, True, locked_bom, lock_record, validation, (), reason)


def lock_summary(validation: Any) -> Mapping:
    """``lockSummary``: the workspace header line (blockers, warnings, whether a lock can be attempted)."""
    if validation is None:
        return deep_freeze({"blockers": 0, "warnings": 0, "canLock": False, "label": "Not validated"})
    blockers, warnings = validation.counts.blocked, validation.counts.warning
    if blockers:
        label = f"{blockers} blocker(s), {warnings} warning(s) — FIX CONFIGURATION"
    elif warnings:
        label = f"{warnings} warning(s) — acknowledgement may be required"
    else:
        label = "All checks passed"
    return deep_freeze({"blockers": blockers, "warnings": warnings, "canLock": blockers == 0, "label": label})


# ---------------------------------------------------------------------------------------------------------------------
# bomStatus.js — "may this configuration proceed?" (domain model only)
# ---------------------------------------------------------------------------------------------------------------------


class BomStatus(StrEnum):
    DRAFT = "DRAFT"
    VALID = "VALID"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"


_STATUS_RULES: Mapping[str, Mapping[str, Any]] = FrozenDict(
    DRAFT=FrozenDict(quotation=False, projectLock=False, reason="Configuration has not been validated."),
    BLOCKED=FrozenDict(quotation=False, projectLock=False, reason="Engineering validation failed. Correct the configuration or obtain an authorised override."),
    WARNING=FrozenDict(quotation=True, projectLock=False, requiresApproval=True, reason="Validation raised warnings. Quotation permitted subject to Project Head approval."),
    VALID=FrozenDict(quotation=True, projectLock=False, reason="Engineering validation passed."),
    APPROVED=FrozenDict(quotation=True, projectLock=True, reason="Approved by engineering. Project BOM may be locked for execution."),
)

#: Legal transitions. APPROVED is reachable only from VALID or WARNING.
_TRANSITIONS: Mapping[str, tuple[str, ...]] = FrozenDict(
    DRAFT=("VALID", "WARNING", "BLOCKED"),
    VALID=("APPROVED", "WARNING", "BLOCKED", "DRAFT"),
    WARNING=("APPROVED", "VALID", "BLOCKED", "DRAFT"),
    BLOCKED=("DRAFT", "VALID", "WARNING"),
    APPROVED=("DRAFT",),
)


def status_from_validation(validation: Any) -> BomStatus:
    """``statusFromValidation``: a checker verdict (object or ``{status}`` mapping) as a BOM status."""
    if validation is None:
        return BomStatus.DRAFT
    status = validation.get("status") if isinstance(validation, Mapping) else getattr(validation, "status", None)
    status = getattr(status, "value", status)
    return BomStatus(status) if status in ("BLOCKED", "WARNING", "VALID") else BomStatus.DRAFT


def _status_rule(status: Any) -> Mapping:
    return _STATUS_RULES.get(status, FrozenDict()) if isinstance(status, str) else FrozenDict()


def can_generate_quotation(status: Any) -> bool:
    return bool(_status_rule(status).get("quotation"))


def can_lock_project_bom(status: Any) -> bool:
    return bool(_status_rule(status).get("projectLock"))


def requires_approval(status: Any) -> bool:
    return bool(_status_rule(status).get("requiresApproval"))


def status_reason(status: Any) -> str:
    return _status_rule(status).get("reason") or "Unknown status."


def can_transition(source: Any, target: Any) -> bool:
    return isinstance(source, str) and target in _TRANSITIONS.get(source, ())


def quotation_gate(status: Any) -> Mapping:
    """``quotationGate``: one answer, with its reason."""
    return deep_freeze({"allowed": can_generate_quotation(status), "requiresApproval": requires_approval(status), "status": status, "reason": status_reason(status)})
