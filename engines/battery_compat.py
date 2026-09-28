"""Battery master data, battery/inverter compatibility and authoritative battery resolution.

Ports of Flarize ``batteryMaster.js`` (master shape and lifecycle — nothing is fabricated: an unknown field is
``None``), ``batteryCompatibility.js`` (checks BC-A … BC-J: a check whose data is missing is INDETERMINATE, never a
pass; missing protection data is safety-critical and BLOCKS) and ``resolveBattery.js`` (identity, quantity and the
dependent accessories: Project Head override → package default → tier shortlist; an unapproved battery is kept and
the configuration BLOCKED, never silently substituted).

Master records keep JavaScript's null/undefined distinction where the JS relied on it (``=== null`` checks).
"""

from __future__ import annotations

import re
from typing import Any

from engines._jscompat import UNDEFINED, clean, is_array, is_nullish, js_join, js_number, js_or, js_str, jsget, nullish, strict_equal, truthy

# batteryMaster.js
ENGINEERING_STATUS = {
    "DRAFT": "DRAFT",
    "PENDING_ENGINEERING_APPROVAL": "PENDING_ENGINEERING_APPROVAL",
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "WITHDRAWN": "WITHDRAWN",
}
PROCUREMENT_STATUS = {"ACTIVE": "ACTIVE", "INACTIVE": "INACTIVE", "DISCONTINUED": "DISCONTINUED", "PENDING_SOURCING": "PENDING_SOURCING"}
BATTERY_PROTECTION_MODE = {"INTEGRATED": "INTEGRATED", "EXTERNAL_REQUIRED": "EXTERNAL_REQUIRED", "EXTERNAL_OPTIONAL": "EXTERNAL_OPTIONAL", "UNKNOWN": "UNKNOWN"}
BATTERY_FIELDS = (
    "componentId",
    "brand",
    "model",
    "displayName",
    "batteryType",
    "chemistry",
    "nominalVoltage",
    "minVoltage",
    "maxVoltage",
    "capacityKwh",
    "usableCapacityKwh",
    "continuousChargeCurrent",
    "continuousDischargeCurrent",
    "maximumChargeCurrent",
    "maximumDischargeCurrent",
    "peakCurrent",
    "integratedProtection",
    "protectionType",
    "protectionRating",
    "externalProtectionRequired",
    "communicationProtocol",
    "communicationRequired",
    "compatibleInverters",
    "compatibleSystemTypes",
    "compatiblePhases",
    "purchasePrice",
    "sellingPrice",
    "supplier",
    "supplierReference",
    "datasheetUrl",
    "engineeringStatus",
    "procurementStatus",
    "createdAt",
    "updatedAt",
)
REQUIRED_FOR_VALIDATION = ("nominalVoltage", "capacityKwh", "maximumDischargeCurrent", "integratedProtection")
REQUIRED_FOR_SAFE_ISSUE = ("integratedProtection",)

# batteryCompatibility.js
RESULT = {"PASS": "PASS", "FAIL": "FAIL", "INDETERMINATE": "INDETERMINATE"}
COMPAT_CHECKS = (
    {"id": "BC-A", "name": "System type compatibility", "needs": ["compatibleSystemTypes"]},
    {"id": "BC-B", "name": "Inverter compatibility", "needs": ["compatibleInverters"]},
    {"id": "BC-C", "name": "Voltage compatibility", "needs": ["nominalVoltage"]},
    {"id": "BC-D", "name": "Current compatibility", "needs": ["maximumDischargeCurrent"]},
    {"id": "BC-E", "name": "Communication compatibility", "needs": ["communicationProtocol"]},
    {"id": "BC-F", "name": "Phase compatibility", "needs": ["compatiblePhases"]},
    {"id": "BC-G", "name": "Protection requirement", "needs": ["integratedProtection"]},
    {"id": "BC-H", "name": "Battery capacity", "needs": ["capacityKwh"]},
    {"id": "BC-I", "name": "Battery quantity", "needs": []},
    {"id": "BC-J", "name": "Manufacturer / architecture compatibility", "needs": []},
)
_CHECK_NAMES = {c["id"]: c["name"] for c in COMPAT_CHECKS}

# resolveBattery.js
SELECTION_METHOD = {
    "PACKAGE_DEFAULT": "PACKAGE_DEFAULT",
    "PROJECT_HEAD_OVERRIDE": "PROJECT_HEAD_OVERRIDE",
    "ENGINEER_PROPOSAL": "ENGINEER_PROPOSAL",
    "PROCUREMENT_SUBSTITUTION": "PROCUREMENT_SUBSTITUTION",
    "FALLBACK": "FALLBACK",
}
BATTERY_PROTECTION_BY_COMPONENT = {
    "bt2": {"mode": "INTEGRATED", "providedBy": "ENERGY_SYSTEM_CONTROLLER", "source": "decision D12 — Enphase premium architecture"},
    "bt1": {"mode": "UNKNOWN", "providedBy": None, "source": "decision D12 — per-battery confirmation still pending"},
    "bt3": {"mode": "UNKNOWN", "providedBy": None, "source": "decision D12 — per-battery confirmation still pending"},
    "bt4": {"mode": "UNKNOWN", "providedBy": None, "source": "decision D12 — per-battery confirmation still pending"},
}
BATTERY_IDENTITY_ALIASES = (
    {
        "canonical": "bt2",
        "aliasCategory": "enphase",
        "aliasNameMatch": re.compile(r"Flex Battery", re.IGNORECASE),
        "note": (
            "Same physical product recorded twice: categories.battery.bt2 and an enphase accessory row. Prices differ. "
            "Canonical record is bt2. The alias is retained for catalog traceability and must NOT be deleted before the "
            "relationship is reviewed (Phase 1G)."
        ),
    },
)


# ---------------------------------------------------------------------------------------------------------------
# batteryMaster.js
# ---------------------------------------------------------------------------------------------------------------


def to_battery_master(item: Any, overlay: Any = None) -> dict | None:
    """A catalog battery row + master-data overlay projected onto the master shape; absent values are ``None``."""
    if not truthy(item):
        return None
    source = {**item, **(overlay if isinstance(overlay, dict) else {})}
    out: dict = {field: source.get(field) for field in BATTERY_FIELDS}
    out["componentId"] = js_or(item.get("id"), source.get("componentId"), None)
    out["brand"] = js_or(source.get("brand"), None)
    out["displayName"] = js_or(source.get("displayName"), source.get("name"), None)
    out["model"] = js_or(source.get("model"), None)
    purchase = source.get("purchasePrice")
    out["purchasePrice"] = purchase if purchase is not None else nullish(jsget(source, "price"), None)
    out["engineeringStatus"] = js_or(source.get("engineeringStatus"), ENGINEERING_STATUS["PENDING_ENGINEERING_APPROVAL"])
    out["procurementStatus"] = js_or(source.get("procurementStatus"), PROCUREMENT_STATUS["ACTIVE"])
    for key in ("compatibleInverters", "compatibleSystemTypes", "compatiblePhases"):
        if out[key] is not None and not is_array(out[key]):
            out[key] = None
    out["tiers"] = item["tiers"] if is_array(item.get("tiers")) else []
    out["missingFields"] = [f for f in BATTERY_FIELDS if out[f] is None]
    out["missingForValidation"] = [f for f in REQUIRED_FOR_VALIDATION if out[f] is None]
    out["missingForSafeIssue"] = [f for f in REQUIRED_FOR_SAFE_ISSUE if out[f] is None]
    return out


def all_batteries(catalog: Any, master_overlay: Any = None) -> list[dict]:
    overlay = master_overlay if isinstance(master_overlay, dict) else {}
    items = jsget(jsget(jsget(catalog, "categories"), "battery"), "items")
    return [to_battery_master(i, js_or(overlay.get(js_str(jsget(i, "id"))), {})) for i in (items if truthy(items) else [])]


def is_selectable(battery: Any) -> bool:
    """Engineering-APPROVED and commercially available."""
    if not truthy(battery):
        return False
    if jsget(battery, "engineeringStatus") != ENGINEERING_STATUS["APPROVED"]:
        return False
    if jsget(battery, "procurementStatus") in (PROCUREMENT_STATUS["INACTIVE"], PROCUREMENT_STATUS["DISCONTINUED"]):
        return False
    status = jsget(battery, "status")
    return not (truthy(status) and status != "ACTIVE")


def is_permanently_unselectable(battery: Any) -> bool:
    """May never appear in a BOM again (REJECTED/WITHDRAWN, INACTIVE/DISCONTINUED, or a non-ACTIVE status)."""
    if not truthy(battery):
        return True
    if jsget(battery, "engineeringStatus") in (ENGINEERING_STATUS["REJECTED"], ENGINEERING_STATUS["WITHDRAWN"]):
        return True
    if jsget(battery, "procurementStatus") in (PROCUREMENT_STATUS["INACTIVE"], PROCUREMENT_STATUS["DISCONTINUED"]):
        return True
    status = jsget(battery, "status")
    return truthy(status) and status != "ACTIVE"


def selectability_reason(battery: Any) -> str | None:
    if not truthy(battery):
        return "component not found"
    status = jsget(battery, "engineeringStatus")
    if status != ENGINEERING_STATUS["APPROVED"]:
        return f"engineeringStatus is {js_str(status)} — a Procurement addition is not selectable until Engineering approves it"
    if jsget(battery, "procurementStatus") == PROCUREMENT_STATUS["INACTIVE"]:
        return "procurementStatus is INACTIVE"
    if jsget(battery, "procurementStatus") == PROCUREMENT_STATUS["DISCONTINUED"]:
        return "procurementStatus is DISCONTINUED"
    return None


# ---------------------------------------------------------------------------------------------------------------
# batteryCompatibility.js
# ---------------------------------------------------------------------------------------------------------------


def _includes(values: Any, item: Any) -> bool:
    if is_array(values):
        return any(strict_equal(v, item) for v in values)
    if isinstance(values, str):
        return js_str(item) in values
    raise TypeError(f"{values!r} has no includes()")


def check_battery_compatibility(battery: Any, ctx: Any = None) -> dict:
    """Checks BC-A … BC-J → ``{status: VALID|WARNING|BLOCKED, checks, reasons, counts}``."""
    ctx = ctx if isinstance(ctx, dict) else {}
    inverter = ctx.get("inverter", None)
    sys_type = ctx.get("sysType", "hybrid")
    phase = ctx.get("phase", None)
    quantity = ctx.get("quantity", 0)
    architecture = ctx.get("architecture", None)
    checks: list[dict] = []

    def add(check_id: str, result: str, message: str, detail: Any = UNDEFINED) -> None:
        checks.append(clean({"id": check_id, "name": _CHECK_NAMES.get(check_id, UNDEFINED), "result": result, "message": message, "detail": detail}))

    if not truthy(battery):
        add("BC-A", RESULT["FAIL"], "No battery component supplied.")
        return _summarise(checks, battery)

    def field(name: str) -> Any:
        return jsget(battery, name)

    bat_brand = js_str(js_or(field("brand"), "")).lower()
    if architecture == "ENPHASE" and bat_brand != "enphase":
        add(
            "BC-J",
            RESULT["FAIL"],
            f'Enphase premium architecture accepts only an Enphase battery. "{js_str(field("displayName"))}" is {js_str(js_or(field("brand"), "unbranded"))}.',
        )
    elif truthy(architecture) and architecture != "ENPHASE" and bat_brand == "enphase":
        add("BC-J", RESULT["FAIL"], f'Enphase battery "{js_str(field("displayName"))}" cannot be used on a {js_str(architecture)} architecture.')
    else:
        add("BC-J", RESULT["PASS"], "Architecture/manufacturer combination is permitted.")

    system_types = field("compatibleSystemTypes")
    if system_types is None:
        add("BC-A", RESULT["INDETERMINATE"], "compatibleSystemTypes not recorded.", {"missing": "compatibleSystemTypes"})
    elif not _includes(system_types, sys_type):
        add("BC-A", RESULT["FAIL"], f'Battery is not listed as compatible with system type "{js_str(sys_type)}".')
    else:
        add("BC-A", RESULT["PASS"], f'Compatible with "{js_str(sys_type)}".')

    inverters = field("compatibleInverters")
    inverter_label = js_str(js_or(jsget(inverter, "name"), jsget(inverter, "id")))
    if inverters is None:
        add("BC-B", RESULT["INDETERMINATE"], "compatibleInverters not recorded — inverter compatibility cannot be verified.", {"missing": "compatibleInverters"})
    elif not truthy(inverter):
        add("BC-B", RESULT["INDETERMINATE"], "No inverter in context to check against.")
    elif not _includes(inverters, jsget(inverter, "id")):
        add("BC-B", RESULT["FAIL"], f'Battery is not approved for inverter "{inverter_label}".', clean({"inverterId": jsget(inverter, "id"), "approved": inverters}))
    else:
        add("BC-B", RESULT["PASS"], f'Approved for inverter "{inverter_label}".')

    voltage = field("nominalVoltage")
    v_min, v_max = jsget(inverter, "batteryVoltageMin"), jsget(inverter, "batteryVoltageMax")
    if voltage is None:
        add("BC-C", RESULT["INDETERMINATE"], "nominalVoltage not recorded.", {"missing": "nominalVoltage"})
    elif not is_nullish(v_min) and not is_nullish(v_max):
        ok = js_number(voltage) >= js_number(v_min) and js_number(voltage) <= js_number(v_max)
        message = "Battery voltage is within the inverter window." if ok else f"Battery {js_str(voltage)} V is outside the inverter window {js_str(v_min)}–{js_str(v_max)} V."
        add("BC-C", RESULT["PASS"] if ok else RESULT["FAIL"], message)
    else:
        add("BC-C", RESULT["INDETERMINATE"], "Inverter battery-voltage window not recorded.", {"missing": "inverter.batteryVoltageMin/Max"})

    current = field("maximumDischargeCurrent")
    required_current = jsget(inverter, "maxBatteryCurrent")
    if current is None:
        add("BC-D", RESULT["INDETERMINATE"], "maximumDischargeCurrent not recorded.", {"missing": "maximumDischargeCurrent"})
    elif not is_nullish(required_current):
        ok = js_number(current) >= js_number(required_current)
        message = "Battery current capability meets the inverter requirement." if ok else f"Battery {js_str(current)} A is below the inverter requirement {js_str(required_current)} A."
        add("BC-D", RESULT["PASS"] if ok else RESULT["FAIL"], message)
    else:
        add("BC-D", RESULT["INDETERMINATE"], "Inverter max battery current not recorded.", {"missing": "inverter.maxBatteryCurrent"})

    protocol = field("communicationProtocol")
    protocols = jsget(inverter, "communicationProtocols")
    if field("communicationRequired") is False:
        add("BC-E", RESULT["PASS"], "No communication link required.")
    elif protocol is None:
        add("BC-E", RESULT["INDETERMINATE"], "communicationProtocol not recorded.", {"missing": "communicationProtocol"})
    elif is_nullish(protocols):
        add("BC-E", RESULT["INDETERMINATE"], "Inverter communication protocols not recorded.", {"missing": "inverter.communicationProtocols"})
    else:
        ok = _includes(protocols, protocol)
        add("BC-E", RESULT["PASS"] if ok else RESULT["FAIL"], f"Protocol {js_str(protocol)} is supported." if ok else f"Inverter does not support {js_str(protocol)}.")

    phases = field("compatiblePhases")
    if phases is None:
        add("BC-F", RESULT["INDETERMINATE"], "compatiblePhases not recorded.", {"missing": "compatiblePhases"})
    elif truthy(phase) and not _includes(phases, phase):
        add("BC-F", RESULT["FAIL"], f'Battery is not listed for phase "{js_str(phase)}".')
    else:
        add("BC-F", RESULT["PASS"], f"Compatible with {js_str(phase)}." if truthy(phase) else "No phase constraint.")

    integrated = field("integratedProtection")
    if integrated is None:
        add(
            "BC-G",
            RESULT["INDETERMINATE"],
            "integratedProtection not recorded — the external-protection requirement cannot be determined.",
            {"missing": "integratedProtection", "safetyCritical": True},
        )
    elif integrated is True:
        add("BC-G", RESULT["PASS"], "Battery carries integrated protection; no external device required.")
    elif field("externalProtectionRequired") is True:
        rating = field("protectionRating")
        if is_nullish(rating):
            add(
                "BC-G",
                RESULT["INDETERMINATE"],
                "External protection is required but protectionRating is not recorded. The rating must not be assumed or derived from Ah.",
                {"missing": "protectionRating", "safetyCritical": True},
            )
        else:
            add("BC-G", RESULT["PASS"], f"External protection required at {js_str(rating)}.")
    else:
        add("BC-G", RESULT["INDETERMINATE"], "externalProtectionRequired not recorded.", {"missing": "externalProtectionRequired"})

    capacity = field("capacityKwh")
    if capacity is None:
        add("BC-H", RESULT["INDETERMINATE"], "capacityKwh not recorded — backup sizing cannot be assessed.", {"missing": "capacityKwh"})
    else:
        add("BC-H", RESULT["PASS"], f"Capacity {js_str(capacity)} kWh recorded.")

    if js_number(quantity) <= 0:
        add("BC-I", RESULT["FAIL"], "Battery quantity must be at least 1 when a battery is selected.")
    else:
        add("BC-I", RESULT["PASS"], f"Quantity {js_str(quantity)}.")
    return _summarise(checks, battery)


def _summarise(checks: list, battery: Any) -> dict:
    failed = [c for c in checks if c["result"] == RESULT["FAIL"]]
    indeterminate = [c for c in checks if c["result"] == RESULT["INDETERMINATE"]]
    safety_critical = [c for c in indeterminate if truthy(jsget(c.get("detail"), "safetyCritical"))]
    missing_safe = js_or(jsget(battery, "missingForSafeIssue"), [])
    status = "VALID"
    reasons = []
    if failed:
        status = "BLOCKED"
        reasons += [f"{c['id']}: {c['message']}" for c in failed]
    elif safety_critical or missing_safe:
        status = "BLOCKED"
        missing = list(dict.fromkeys([c["detail"]["missing"] for c in safety_critical] + list(missing_safe)))
        reasons.append("Cannot safely validate this battery configuration — " + js_join(missing, ", ") + " missing.")
    elif indeterminate:
        status = "WARNING"
        missing = list(dict.fromkeys(m for m in (jsget(c.get("detail"), "missing") for c in indeterminate) if truthy(m)))
        reasons.append("Engineering data incomplete — review required: " + js_join(missing, ", "))
    return {
        "status": status,
        "checks": checks,
        "reasons": reasons,
        "counts": {"fail": len(failed), "indeterminate": len(indeterminate), "pass": len(checks) - len(failed) - len(indeterminate)},
    }


def resolve_protection_requirement(battery: Any) -> dict:
    """Protection topology from master data; a rating is never assumed."""
    if not truthy(battery):
        return {"mode": "UNKNOWN", "externalDeviceRequired": False, "rating": None, "reason": "no battery"}
    if jsget(battery, "integratedProtection") is True:
        return {"mode": "INTEGRATED", "externalDeviceRequired": False, "rating": None, "reason": "battery carries integrated protection"}
    if jsget(battery, "externalProtectionRequired") is True:
        rating = nullish(jsget(battery, "protectionRating"), None)
        return {
            "mode": "EXTERNAL_REQUIRED",
            "externalDeviceRequired": True,
            "rating": rating,
            "reason": ("external protection required; rating NOT recorded and must not be assumed" if rating is None else f"external protection required at {js_str(rating)}"),
        }
    if jsget(battery, "externalProtectionRequired") is False:
        return {"mode": "EXTERNAL_OPTIONAL", "externalDeviceRequired": False, "rating": None, "reason": "external protection explicitly not required"}
    return {"mode": "UNKNOWN", "externalDeviceRequired": False, "rating": None, "reason": "protection data not recorded"}


# ---------------------------------------------------------------------------------------------------------------
# resolveBattery.js
# ---------------------------------------------------------------------------------------------------------------


def _local_active(item: Any) -> bool:
    if not truthy(item):
        return False
    status = jsget(item, "status")
    if truthy(status) and status != "ACTIVE":
        return False
    tiers = jsget(item, "tiers")
    return is_array(tiers) and len(tiers) > 0


def approved_battery_shortlist(catalog: Any, master: Any = None, ctx: Any = None) -> dict:
    """``{selectable: [master records], rejected: [{componentId, reason}]}`` for a tier — nothing hidden silently."""
    master = master if isinstance(master, dict) else {}
    tier = (ctx if isinstance(ctx, dict) else {}).get("tier", None)
    items = jsget(jsget(jsget(catalog, "categories"), "battery"), "items")
    selectable, rejected = [], []
    for item in items if truthy(items) else []:
        record = to_battery_master(item, js_or(master.get(js_str(jsget(item, "id"))), {}))
        tiers = jsget(item, "tiers")
        if truthy(tier) and not (is_array(tiers) and _includes(tiers, tier)):
            rejected.append(clean({"componentId": jsget(item, "id"), "reason": f'not offered in tier "{js_str(tier)}"'}))
            continue
        if not _local_active(item):
            rejected.append(clean({"componentId": jsget(item, "id"), "reason": f"catalog status is {js_str(jsget(item, 'status'))}"}))
            continue
        if not is_selectable(record):
            rejected.append(clean({"componentId": jsget(item, "id"), "reason": selectability_reason(record)}))
            continue
        selectable.append(record)
    return {"selectable": selectable, "rejected": rejected}


def _is_project_quantity(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1, 2)


def resolve_battery(profile: Any, catalog: Any, context: Any = None) -> dict:
    """Battery identity, quantity, protection and dependent accessories for a package (see module docstring)."""
    context = context if isinstance(context, dict) else {}
    sys_type = context.get("sysType", "ongrid")
    tier = context.get("tier", "value")
    master = js_or(context.get("batteryMaster", None), None)
    project_override = context.get("projectOverride", None)
    project_qty = context.get("projectBatteryQuantity", None)
    issues: list[dict] = []
    have_master = truthy(master) and isinstance(master, dict) and len(master) > 0

    declared_qty = js_number(nullish(jsget(profile, "batteryQuantity"), 0))
    declared_included = truthy(jsget(profile, "batteryIncluded"))
    valid_project_qty = _is_project_quantity(project_qty)
    effective_qty = project_qty if valid_project_qty else declared_qty
    quantity = effective_qty if sys_type == "hybrid" else 0
    if sys_type != "hybrid":
        quantity_source = "NONE"
    else:
        quantity_source = "PROJECT_HEAD_OVERRIDE" if valid_project_qty else "PROFILE_DEFAULT"

    if declared_included and declared_qty == 0:
        issues.append({"code": "PROFILE_INCONSISTENT", "message": "Profile sets batteryIncluded=true but batteryQuantity=0."})
    if not declared_included and declared_qty > 0:
        issues.append({"code": "PROFILE_INCONSISTENT", "message": f"Profile sets batteryIncluded=false but batteryQuantity={js_str(declared_qty)}."})
    if sys_type != "hybrid" and declared_qty > 0:
        issues.append({"code": "BATTERY_ON_NON_HYBRID", "message": f'Profile declares a battery for system type "{js_str(sys_type)}". Ignored.'})
    if sys_type == "hybrid" and valid_project_qty and project_qty != declared_qty:
        issues.append(
            {
                "code": "BATTERY_QUANTITY_PROJECT_OVERRIDE",
                "message": f"Project Head battery quantity {js_str(project_qty)} overrides profile default {js_str(declared_qty)}. Engineering must confirm compatibility.",
            }
        )

    required = js_number(quantity) > 0
    component = None
    source = "none"
    selection_method = None
    items = jsget(jsget(jsget(catalog, "categories"), "battery"), "items")
    batteries = items if truthy(items) else []

    def record_for(item: Any) -> dict | None:
        if not truthy(item):
            return None
        overlay = js_or(jsget(master, jsget(item, "id")), {}) if truthy(master) else {}
        return to_battery_master(item, overlay)

    def gate_ok(item: Any) -> bool:
        if not _local_active(item):
            return False
        return True if not have_master else is_selectable(record_for(item))

    def resolvable_ok(item: Any) -> bool:
        if not _local_active(item):
            return False
        return True if not have_master else not is_permanently_unselectable(record_for(item))

    def find(component_id: Any) -> Any:
        return next((b for b in batteries if strict_equal(jsget(b, "id"), component_id)), None)

    if required:
        override_id = jsget(project_override, "componentId")
        if truthy(override_id):
            candidate = find(override_id)
            if candidate is None:
                issues.append(
                    {
                        "code": "BATTERY_OVERRIDE_NOT_FOUND",
                        "message": f'Project override battery "{js_str(override_id)}" is not in categories.battery. BLOCKED — no substitution is made.',
                    }
                )
            elif not gate_ok(candidate):
                reason = js_or(selectability_reason(record_for(candidate)), "catalog status")
                issues.append(
                    {
                        "code": "BATTERY_OVERRIDE_NOT_SELECTABLE",
                        "message": f'Project override battery "{js_str(jsget(candidate, "name"))}" is not selectable: {js_str(reason)}. BLOCKED — no automatic substitution.',
                    }
                )
                component, source, selection_method = candidate, "projectOverride(blocked)", SELECTION_METHOD["PROJECT_HEAD_OVERRIDE"]
            else:
                component, source, selection_method = candidate, "projectOverride", SELECTION_METHOD["PROJECT_HEAD_OVERRIDE"]
        profile_battery = jsget(profile, "batteryComponentId")
        if component is None and truthy(profile_battery):
            component = find(profile_battery)
            if component is not None:
                source, selection_method = "profile.batteryComponentId", SELECTION_METHOD["PACKAGE_DEFAULT"]
            else:
                issues.append({"code": "BATTERY_COMPONENT_NOT_FOUND", "message": f'Profile batteryComponentId "{js_str(profile_battery)}" is not in categories.battery.'})
        if component is None:
            matches = [b for b in batteries if _includes(js_or(jsget(b, "tiers"), []), tier) and resolvable_ok(b)]
            if len(matches) == 1:
                component, source, selection_method = matches[0], f"catalog.battery[tier={js_str(tier)}]", SELECTION_METHOD["PACKAGE_DEFAULT"]
            elif len(matches) > 1:
                component, source, selection_method = matches[0], f"catalog.battery[tier={js_str(tier)}][0]", SELECTION_METHOD["FALLBACK"]
                issues.append(
                    {
                        "code": "BATTERY_AMBIGUOUS",
                        "message": (
                            f'{len(matches)} batteries available for tier "{js_str(tier)}"; the package does not name one. '
                            f'Chose "{js_str(jsget(component, "name"))}". Define batteryComponentId on the profile.'
                        ),
                    }
                )
        if component is None:
            issues.append({"code": "BATTERY_NO_CANDIDATE", "message": f'No usable battery in categories.battery for tier "{js_str(tier)}".'})

    record = record_for(component)
    engineering_approved = True if not required else (True if not have_master else jsget(record, "engineeringStatus") == ENGINEERING_STATUS["APPROVED"])
    label = js_str(js_or(jsget(component, "name"), jsget(component, "id")))
    if required and have_master and not engineering_approved:
        open_items = js_or(jsget(jsget(master, jsget(component, "id")), "openItems"), [])
        issues.append(
            {
                "code": "BATTERY_NOT_ENGINEERING_APPROVED",
                "message": (
                    f'Battery "{label}" is {js_str(jsget(record, "engineeringStatus"))}. It is retained in the BOM so the system is not silently '
                    "shipped without a battery, but the configuration is BLOCKED until Engineering approves it. "
                    f"Open items: {js_or(js_join(open_items, ', '), 'see battery master data')}."
                ),
            }
        )

    if have_master and record is not None:
        requirement = resolve_protection_requirement(record)
        protection = {
            "mode": requirement["mode"],
            "providedBy": js_or(record.get("protectionType"), None),
            "rating": requirement["rating"],
            "source": f"battery master data ({js_str(record.get('componentId'))})",
            "reason": requirement["reason"],
        }
    else:
        legacy = BATTERY_PROTECTION_BY_COMPONENT.get(js_str(jsget(component, "id"))) if component is not None else None
        protection = {**(legacy or {"mode": "UNKNOWN", "providedBy": None, "source": "no decision recorded for this component"}), "rating": None}
    requires_external = required and protection["mode"] in ("EXTERNAL_REQUIRED", "UNKNOWN")
    if required and protection["mode"] == "UNKNOWN":
        issues.append(
            {
                "code": "BATTERY_PROTECTION_MODE_UNKNOWN",
                "message": f'Battery protection mode for "{label}" is not confirmed. Existing template behaviour preserved. Engineering confirmation required (D12).',
            }
        )
    if required and protection["mode"] == "EXTERNAL_REQUIRED" and protection["rating"] is None:
        issues.append(
            {
                "code": "BATTERY_PROTECTION_RATING_UNRECORDED",
                "message": (
                    f'External battery protection is required for "{label}" but the RATING is not recorded. ' "It must not be assumed or derived from Ah. Engineering verification pending (D12)."
                ),
            }
        )
    return {
        "protection": {**protection, "requiresExternalProtection": requires_external, "requiresGenericDcCable": requires_external},
        "quantity": quantity,
        "required": required,
        "componentId": js_or(jsget(component, "id"), None),
        "component": component,
        "master": record,
        "category": "battery",
        "selectionMethod": selection_method,
        "approvalStatus": js_or(jsget(record, "engineeringStatus"), None),
        "engineeringApproved": engineering_approved,
        "missingForSafeIssue": js_or(jsget(record, "missingForSafeIssue"), []),
        "selectedBy": js_or(jsget(project_override, "selectedBy"), None),
        "selectionTimestamp": js_or(jsget(project_override, "at"), None),
        "spec": {
            "brand": js_or(jsget(component, "brand"), jsget(profile, "batteryBrand"), None),
            "model": js_or(jsget(component, "model"), jsget(profile, "batteryModel"), None),
            "capacityKwh": nullish(jsget(component, "kwh"), jsget(profile, "batteryCapacity"), None),
            "unitPrice": nullish(jsget(component, "price"), None),
        },
        "requiresMccb": requires_external,
        "requiresCable": requires_external,
        "requiresChangeOver": required,
        "batQtyIndex": js_str(quantity),
        "source": source,
        "quantitySource": quantity_source,
        "profileDeclaredQuantity": declared_qty,
        "projectOverrideQuantity": project_qty if valid_project_qty else None,
        "issues": issues,
    }


def is_battery_alias(category_key: Any, name: Any) -> bool:
    """Is this BOM line the alias identity of a canonical battery (e.g. the enphase "Flex Battery" row)?"""
    return any(a["aliasCategory"] == category_key and a["aliasNameMatch"].search(js_str(js_or(name, ""))) for a in BATTERY_IDENTITY_ALIASES)


def canonical_battery_id_for(category_key: Any, name: Any) -> str | None:
    hit = next((a for a in BATTERY_IDENTITY_ALIASES if a["aliasCategory"] == category_key and a["aliasNameMatch"].search(js_str(js_or(name, "")))), None)
    return hit["canonical"] if hit else None
