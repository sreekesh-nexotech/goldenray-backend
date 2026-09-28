"""Quotation payload assembly, commercial snapshots, the commercial freeze and the validity policy (pure).

Ports of the Flarize modules that produce what an issued quotation version freezes (PLAN §2.6
``quotations_version.document_payload`` / ``gate_report``, ``quotations_commercial_snapshot``; engines spec §15, §19;
workflows spec C.1, C.6, C.7, F.3):

* ``quotationPayload.js`` — :func:`build_quotation_payload`: copies identity and quantity out of the LOCKED BOM
  snapshot and every commercial figure out of the ISSUED commercial snapshot; a missing source becomes a named blocked
  section, never a zero or a default. No money arithmetic (only the display figures the renderer must not compute).
* ``quotationWorkspace.js`` (pure parts) — :func:`component_attributes_for`, :func:`resolve_panel_dcr_type`,
  :func:`alternative_inputs`, :func:`project_issued_payload_for_actor`, :func:`project_issued_snapshot_for_actor`,
  :func:`verify_immutability`, :func:`normalize_renderer_pinning`, :func:`issued_document`.
* ``commercialSnapshot.js`` — :func:`create_commercial_snapshot`, :func:`create_pack_commercial_snapshot` (13 pinned
  versions, :data:`VERSION_KEYS`), :func:`apply_config_change`, :func:`compare_recalculation`, :func:`supersede`.
* ``commercialFreeze.js`` — :func:`build_commercial_snapshot` (``document.snapshot``, 10 domains, production guards).
* ``quotationPolicy.js`` — :func:`resolve_effective_validity` and the policy store (override → effective policy →
  admin default → ``POLICY_NOT_CONFIGURED``).

Every result is deep-frozen (:mod:`engines.frozen`): no live reference to master data survives into a frozen
document. Inputs are JSON-shaped mappings with the legacy (camelCase) keys; missing members stay missing (JavaScript
``undefined`` is dropped, as ``JSON.stringify`` drops it), so a stored payload is the legacy payload key for key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any

from engines.bom_domain import Role, role_for_line
from engines.content_fit import demo_branding_kinds, freeze_branding_snapshot
from engines.frozen import FrozenDict, deep_freeze, is_frozen, thaw
from engines.gate import SubsidyTreatment, evaluate_generation_gate
from engines.jscompat import (
    EXACT,
    UNDEFINED,
    coalesce,
    format_en_in,
    is_nullish,
    is_number,
    iso_from_ms,
    js_array,
    js_keys,
    js_number,
    js_round,
    js_string,
    js_trim,
    js_truthy,
    number_text,
    parse_iso_ms,
    prop,
    round_places,
    utf16_len,
)

__all__ = [
    "QUOTATION_PAYLOAD_VERSION",
    "IDENTITY_MODEL",
    "Source",
    "Dependency",
    "Treatment",
    "QuotationStatus",
    "VersionStatus",
    "REQUIRED_VERSION_KEYS",
    "VERSION_KEYS",
    "CUSTOMER_FACING_ROLES",
    "INTERNAL_ONLY_ROLES",
    "DISPLAY_CATEGORY",
    "ALLOWED_ATTRIBUTE_KEYS",
    "CAPABILITY_COST_VIEW",
    "blocked_section",
    "build_quotation_payload",
    "build_quotation_payload_from",
    "component_attributes_for",
    "resolve_panel_dcr_type",
    "panel_unresolved_subsidy_result",
    "alternative_inputs",
    "alternative_entry",
    "with_alternatives",
    "project_issued_payload_for_actor",
    "project_issued_snapshot_for_actor",
    "verify_immutability",
    "RendererPinningInvalid",
    "normalize_renderer_pinning",
    "issued_document",
    "CommercialSnapshotStatus",
    "SnapshotError",
    "create_commercial_snapshot",
    "create_pack_commercial_snapshot",
    "apply_config_change",
    "compare_recalculation",
    "supersede",
    "FreezeError",
    "build_commercial_snapshot",
    "PolicyError",
    "PolicyErrorCode",
    "validate_policy_record",
    "resolve_active_policy",
    "resolve_effective_validity",
    "publish_policy",
    "set_policy_status",
    "set_default_validity_days",
    "describe_policy_store",
]

QUOTATION_PAYLOAD_VERSION = "quotationPayload.1"
#: ``quotationWorkspace.IDENTITY_MODEL`` — the legacy label every document carries.
IDENTITY_MODEL = "OPERATOR_ASSERTED_ROLE"
CAPABILITY_COST_VIEW = "COST_VIEW"


class Source(StrEnum):
    """Where a section's data came from. Every emitted section names one."""

    LOCKED_BOM_SNAPSHOT = "LOCKED_BOM_SNAPSHOT"
    COMMERCIAL_SNAPSHOT = "COMMERCIAL_SNAPSHOT"
    ENGINEERING_CHECKER = "ENGINEERING_CHECKER"
    PROJECT_CONFIGURATION = "PROJECT_CONFIGURATION"
    QUOTATION_RECORD = "QUOTATION_RECORD"
    CUSTOMER_RECORD = "CUSTOMER_RECORD"
    COMPONENT_DISPLAY_ATTRIBUTES = "COMPONENT_DISPLAY_ATTRIBUTES"
    UPGRADE_MODEL = "UPGRADE_MODEL"


class Dependency(StrEnum):
    """Named dependencies of blocked sections (emitted as blockers, never as zero)."""

    SUBSIDY_ENGINE = "SUBSIDY_ENGINE"
    SAVINGS_ENGINE = "SAVINGS_ENGINE"
    FINANCE_ENGINE = "FINANCE_ENGINE"
    ENERGY_LOAD_CALCULATION_ENGINE = "ENERGY_LOAD_CALCULATION_ENGINE"
    CMS = "CMS"
    COMPANY_MASTER = "COMPANY_MASTER"
    DECISION_C6_TIER_DISPLAY_NAMES = "DECISION_C6_TIER_DISPLAY_NAMES"
    DECISION_C7_COMPANY_IDENTITY = "DECISION_C7_COMPANY_IDENTITY"
    COMMERCIAL_SNAPSHOT = "COMMERCIAL_SNAPSHOT"
    LOCKED_BOM_SNAPSHOT = "LOCKED_BOM_SNAPSHOT"
    COMPONENT_DISPLAY_ATTRIBUTES = "COMPONENT_DISPLAY_ATTRIBUTES"


class Treatment(StrEnum):
    INCLUDED = "INCLUDED"
    EXTRA = "EXTRA"
    OPTIONAL = "OPTIONAL"
    EXCLUDED = "EXCLUDED"


class QuotationStatus(StrEnum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    SUPERSEDED = "SUPERSEDED"


class VersionStatus(StrEnum):
    ISSUED = "ISSUED"
    SUPERSEDED = "SUPERSEDED"


#: The thirteen versions a commercial snapshot pins (C69 / C93).
VERSION_KEYS = (
    "bomSnapshotId",
    "catalogVersion",
    "procurementPriceVersion",
    "landedCostVersion",
    "projectRateCardVersion",
    "costConfigVersion",
    "marginVersion",
    "gstVersion",
    "costEngineVersion",
    "pricingEngineVersion",
    "moneyRuleVersion",
    "offerVersion",
    "marketRateVersion",
)
REQUIRED_VERSION_KEYS = VERSION_KEYS
_INTERNAL_VERSION_KEYS = frozenset({"landedCostVersion", "marginVersion", "procurementPriceVersion"})

#: Contract §6 / §16 — the BOM roles that reach the customer document.
CUSTOMER_FACING_ROLES = (
    Role.PANEL,
    Role.INVERTER,
    Role.MICRO_INVERTER,
    Role.BATTERY,
    Role.BATTERY_PROTECTION,
    Role.STRUCTURE,
    Role.DCDB,
    Role.ACDB,
    Role.DC_CABLE,
    Role.AC_CABLE,
    Role.EARTHING,
    Role.PROTECTION,
    Role.MONITORING,
    Role.METER,
    Role.AC_ISOLATOR,
    Role.CHANGEOVER,
    Role.ENERGY_SYSTEM_CONTROLLER,
)
INTERNAL_ONLY_ROLES = (Role.BATTERY_CABLE, Role.ENPHASE_COMMUNICATION, Role.OTHER)

#: Contract §6 display categories (grouping labels only).
DISPLAY_CATEGORY: Mapping[str, str] = FrozenDict(
    {
        Role.PANEL.value: "Solar Module",
        Role.INVERTER.value: "Inverter",
        Role.MICRO_INVERTER.value: "Micro Inverter",
        Role.BATTERY.value: "Battery",
        Role.BATTERY_PROTECTION.value: "Battery Protection",
        Role.STRUCTURE.value: "Mounting Structure",
        Role.DCDB.value: "DCDB",
        Role.ACDB.value: "ACDB",
        Role.DC_CABLE.value: "DC Cable",
        Role.AC_CABLE.value: "AC Cable",
        Role.EARTHING.value: "Earthing",
        Role.PROTECTION.value: "Lightning Protection",
        Role.MONITORING.value: "Monitoring",
        Role.METER.value: "Meter",
        Role.AC_ISOLATOR.value: "AC Isolator",
        Role.CHANGEOVER.value: "Change Over",
        Role.ENERGY_SYSTEM_CONTROLLER.value: "Energy System Controller",
    }
)

#: Display attributes the payload may carry; anything commercial is refused by construction (§17, §30, C3).
ALLOWED_ATTRIBUTE_KEYS = ("brand", "model", "technology", "specification", "capacity", "moduleWatt", "phase", "crossSection", "unit", "dcr", "warranty")
_FORBIDDEN_ATTRIBUTE = re.compile(r"price|cost|rate|margin|discount|supplier|purchase", re.IGNORECASE | re.ASCII)

_BOM_SNAPSHOT_LOCKED = "LOCKED"
_COMMERCIAL_SNAPSHOT_ISSUED = "ISSUED"
_TIERS = ("base", "value", "premium")


# ---------------------------------------------------------------------------------------------------------------------
# JavaScript object helpers
# ---------------------------------------------------------------------------------------------------------------------


def _spread(value: Any) -> dict:
    """``{...value}``: an object's own members (a string spreads to its characters, other scalars to nothing)."""
    if isinstance(value, Mapping):
        return {key: value[key] for key in js_keys(value)}
    if isinstance(value, str):
        return {str(index): character for index, character in enumerate(value)}
    if isinstance(value, (list, tuple)):
        return {str(index): item for index, item in enumerate(value)}
    return {}


def _is_js_object(value: Any) -> bool:
    return isinstance(value, (Mapping, list, tuple))


def _nullable(value: Any) -> Any:
    """``value ?? null``."""
    return coalesce(value, None)


def _trimmed_or_null(value: Any) -> str | None:
    return js_trim(value) if isinstance(value, str) and js_trim(value) else None


# ---------------------------------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------------------------------


def blocked_section(*, dependency: Any, reference: Any = None, reason: str) -> dict[str, Any]:
    """The single shape of an unavailable section: ``value`` is always null."""
    return {"available": False, "value": None, "dependency": dependency, "reference": _nullable(reference), "reason": reason}


def _carried_section(*, source: Any, reference: Any, value: Any) -> dict[str, Any]:
    return {"available": True, "value": value, "source": source, "reference": _nullable(reference), "dependency": None, "reason": None}


def _safe_attributes(raw: Any) -> tuple[dict, bool, list]:
    if not _is_js_object(raw):
        return {}, False, []
    out, refused = {}, []
    for key, value in _spread(raw).items():
        if _FORBIDDEN_ATTRIBUTE.search(key) and key != "crossSection":
            refused.append(key)
            continue
        if key not in ALLOWED_ATTRIBUTE_KEYS:
            continue
        out[key] = value
    return out, bool(out), refused


def _attribute_lookup(attributes: Any, component_id: Any) -> Any:
    if not isinstance(attributes, Mapping):
        return UNDEFINED
    return attributes.get(component_id if isinstance(component_id, str) else js_string(component_id), UNDEFINED)


def _build_rows(bom_snapshot: Mapping, component_attributes: Any) -> list[dict[str, Any]]:
    rows = []
    for line in js_array(prop(bom_snapshot, "lines")) or []:
        role = prop(line, "role") if js_truthy(prop(line, "role")) else role_for_line(line).value
        component_id = coalesce(prop(line, "componentId"), prop(line, "itemId"), prop(line, "catalogItemId"), None)
        attributes, found, refused = _safe_attributes(_attribute_lookup(component_attributes, component_id))
        warranty = attributes.get("warranty", UNDEFINED)
        rows.append(
            {
                "componentId": component_id,
                "componentDataVersion": _nullable(prop(line, "componentDataVersion")),
                "role": role,
                "displayCategory": _nullable(DISPLAY_CATEGORY.get(role) if isinstance(role, str) else None),
                "quantity": _nullable(prop(line, "quantity")),
                "treatment": Treatment.INCLUDED.value,
                "customerFacing": role in CUSTOMER_FACING_ROLES,
                "identitySource": Source.LOCKED_BOM_SNAPSHOT.value,
                "quantitySource": Source.LOCKED_BOM_SNAPSHOT.value,
                "engineeringApprovalState": _nullable(prop(line, "engineeringApprovalState")),
                "attributesFound": found,
                "attributesSource": Source.COMPONENT_DISPLAY_ATTRIBUTES.value if found else None,
                "attributesRefused": refused,
                "attributes": attributes,
                "warranty": (
                    _carried_section(source=Source.COMPONENT_DISPLAY_ATTRIBUTES.value, reference="§15", value=warranty)
                    if not is_nullish(warranty)
                    else blocked_section(
                        dependency=Dependency.COMPONENT_DISPLAY_ATTRIBUTES.value,
                        reference="§15",
                        reason="No component warranty data supplied. §15 forbids a generic warranty statement, so none is emitted.",
                    )
                ),
            }
        )
    return rows


def _issued(commercial_snapshot: Any) -> bool:
    return js_truthy(commercial_snapshot) and prop(commercial_snapshot, "status") == _COMMERCIAL_SNAPSHOT_ISSUED


def _build_pricing(commercial_snapshot: Any, include_internal: bool) -> dict[str, Any]:
    if not _issued(commercial_snapshot):
        return {
            "available": False,
            "source": None,
            "customer": None,
            "internal": None,
            "blocked": blocked_section(
                dependency=Dependency.COMMERCIAL_SNAPSHOT.value,
                reference="§17",
                reason="No ISSUED commercial snapshot supplied. No price exists; none is invented and none is zeroed.",
            ),
        }
    pricing = prop(commercial_snapshot, "pricing")
    discount = prop(pricing, "discount")
    customer = {
        "sellingPriceBeforeGST": prop(pricing, "sellingPriceBeforeGST"),
        "gstRatePct": prop(pricing, "gstRatePct"),
        "gstAmount": prop(pricing, "gstAmount"),
        "sellingPriceIncludingGST": prop(pricing, "sellingPriceIncludingGST"),
        "extrasIncludingGST": prop(pricing, "extrasIncludingGST"),
        "customerTotalIncludingGST": prop(pricing, "customerTotalIncludingGST"),
        "gstAppliedOnce": True,
        "gstReference": "C67 / C73 — SOLAR_70_30_COMPOSITE, applied once on the pre-GST selling price.",
        "discount": (
            _carried_section(source=Source.COMMERCIAL_SNAPSHOT.value, reference="§17", value=discount)
            if not is_nullish(discount)
            else blocked_section(
                dependency=Dependency.COMMERCIAL_SNAPSHOT.value,
                reference="§17",
                reason="The commercial snapshot pins no discount field. The discount is not re-derived here.",
            )
        ),
    }
    internal = (
        {
            "marginType": prop(pricing, "marginType"),
            "targetGrossMargin": prop(pricing, "targetGrossMargin"),
            "grossProfit": prop(pricing, "grossProfit"),
            "cost": prop(commercial_snapshot, "cost"),
            "capabilityRequired": CAPABILITY_COST_VIEW,
        }
        if include_internal
        else None
    )
    return {
        "available": True,
        "source": Source.COMMERCIAL_SNAPSHOT.value,
        "snapshotId": prop(commercial_snapshot, "snapshotId"),
        "customer": customer,
        "internal": internal,
        "internalWithheld": not include_internal,
        "internalWithheldReason": None if include_internal else "Internal cost and margin require COST_VIEW (§17, §30). They are absent from this payload, not masked.",
    }


def _build_extras(commercial_snapshot: Any) -> dict[str, Any]:
    if not _issued(commercial_snapshot):
        return {
            "available": False,
            "items": [],
            "blocked": blocked_section(
                dependency=Dependency.COMMERCIAL_SNAPSHOT.value,
                reference="§18",
                reason="Extras are priced by the Pricing Engine and pinned on the commercial snapshot. None supplied.",
            ),
        }
    pricing = prop(commercial_snapshot, "pricing")
    items = [
        {
            "extraId": _nullable(prop(extra, "extraId")),
            "description": _nullable(prop(extra, "description")),
            "treatment": Treatment.EXTRA.value,
            "includedInBase": False,
            "customerPriceExcludingGST": _nullable(prop(extra, "customerPriceExcludingGST")),
            "gstRatePct": _nullable(prop(extra, "gstRatePct")),
            "customerPriceIncludingGST": _nullable(prop(extra, "customerPriceIncludingGST")),
            "source": Source.COMMERCIAL_SNAPSHOT.value,
        }
        for extra in (js_array(prop(pricing, "extras")) or [])
    ]
    customer_side = [
        {
            **_spread(expense),
            "treatment": Treatment.EXCLUDED.value,
            "reference": "C68 / C90",
            "note": "Customer-procured. Excluded from the BOM, cost, margin and selling price.",
            "source": Source.COMMERCIAL_SNAPSHOT.value,
        }
        for expense in (js_array(prop(pricing, "customerSideExpenses")) or [])
    ]
    return {"available": True, "source": Source.COMMERCIAL_SNAPSHOT.value, "items": items, "customerSideExpenses": customer_side}


def _build_subsidy_section(subsidy_treatment: Any, subsidy_result: Any) -> dict[str, Any]:
    if subsidy_treatment == SubsidyTreatment.NOT_QUOTED:
        return {
            "available": False,
            "value": None,
            "treatment": SubsidyTreatment.NOT_QUOTED.value,
            "dependency": Dependency.SUBSIDY_ENGINE.value,
            "reference": "§20",
            "reason": "No Subsidy Engine exists. The operator has recorded NOT_QUOTED: this quotation carries no subsidy. It is not assumed to be zero.",
        }
    if js_truthy(subsidy_result) and js_truthy(prop(subsidy_result, "available")):
        return {
            "available": True,
            "treatment": coalesce(prop(subsidy_result, "treatment"), SubsidyTreatment.ENGINE_RESULT.value),
            "source": _nullable(prop(subsidy_result, "source")),
            "status": _nullable(prop(subsidy_result, "status")),
            "value": _spread(subsidy_result),
            "dependency": None,
            "reference": "§20",
            "reason": None,
        }
    if js_truthy(subsidy_result) and _is_js_object(subsidy_result):
        return {
            "available": False,
            "treatment": coalesce(prop(subsidy_result, "treatment"), SubsidyTreatment.ENGINE_RESULT.value),
            "source": _nullable(prop(subsidy_result, "source")),
            "status": _nullable(prop(subsidy_result, "status")),
            "eligibilityStatus": _nullable(prop(subsidy_result, "eligibilityStatus")),
            "subsidyType": _nullable(prop(subsidy_result, "subsidyType")),
            "value": None,
            "dependency": None,
            "reference": "§20",
            "reason": coalesce(prop(subsidy_result, "reason"), "Subsidy engine returned no subsidy for this configuration."),
        }
    return blocked_section(
        dependency=Dependency.SUBSIDY_ENGINE.value,
        reference="§20",
        reason="No Subsidy Engine and no recorded subsidy treatment. No eligibility, amount or post-subsidy figure exists.",
    )


def _tier_text(tier: Any) -> str:
    return js_string(tier if js_truthy(tier) else "").lower()


def _tier_display_name(package_tier: Any, names: Any) -> dict[str, Any]:
    if not js_truthy(names) or not _is_js_object(names):
        return blocked_section(dependency=Dependency.DECISION_C6_TIER_DISPLAY_NAMES.value, reference="§5", reason="No tier display name configuration supplied.")
    tier = _tier_text(package_tier)
    name = prop(names, tier)
    if js_truthy(name) and isinstance(name, str):
        return {"available": True, "value": name, "source": "BUSINESS_DECISION_C6"}
    return blocked_section(dependency=Dependency.DECISION_C6_TIER_DISPLAY_NAMES.value, reference="§5", reason=f'No display name configured for tier "{tier}".')


def _recommended_badge(package_tier: Any, names: Any) -> str | None:
    if not js_truthy(names) or not _is_js_object(names):
        return None
    recommended = _tier_text(prop(names, "recommendedTier"))
    if not recommended or recommended != _tier_text(package_tier):
        return None
    badge = prop(names, "recommendedBadge")
    return badge if js_truthy(badge) and isinstance(badge, str) else None


def _inclusion_matrix(package_tier: Any, matrix: Any) -> dict[str, Any]:
    if not js_truthy(matrix) or not _is_js_object(matrix):
        return blocked_section(dependency="INCLUSION_MATRIX", reference="§INCLUSION", reason="No inclusion matrix configuration supplied.")
    tier = _tier_text(package_tier)
    members = _spread(matrix)
    config_key = next(
        (key for key in members if not key.startswith("_") and (key.lower() == tier or js_string(prop(members[key], "_tierCode") or "").lower() == tier)),
        None,
    )
    tier_config = members.get(config_key) if config_key is not None else None
    if not js_truthy(tier_config) or not _is_js_object(tier_config):
        return blocked_section(dependency="INCLUSION_MATRIX", reference="§INCLUSION", reason=f'No inclusion configuration for tier "{tier}".')
    items = {key: (value if isinstance(value, bool) else None) for key, value in _spread(tier_config).items() if not key.startswith("_")}
    raw_matrix = js_array(members.get("_serviceMatrix"))
    out: dict[str, Any] = {"available": True, "tier": config_key, "items": items}
    if raw_matrix is not None:
        rows = []
        for row in raw_matrix:
            if not js_truthy(row) or not _is_js_object(row) or not isinstance(prop(row, "label"), str) or not js_trim(row["label"]):
                continue
            value = prop(row, tier)
            rows.append({"label": row["label"], "value": value if isinstance(value, bool) else (value if isinstance(value, str) and js_trim(value) else None)})
        out["serviceMatrix"] = rows
    out["source"] = "COMMERCIAL_INCLUSION_CONFIG"
    return out


def _finite_number_or_null(value: Any) -> Decimal | None:
    """``Number.isFinite(Number(v)) && v !== null && v !== '' ? Number(v) : null``."""
    if value is None or value == "":
        return None
    number = js_number(value)
    return number if number.is_finite() else None


def _testimonials(content: Any) -> dict[str, Any]:
    entries = js_array(prop(content, "entries")) if js_truthy(content) else None
    clean = []
    for entry in entries or []:
        if not (js_truthy(entry) and _is_js_object(entry) and isinstance(prop(entry, "name"), str) and js_trim(entry["name"])):
            continue
        if not (isinstance(prop(entry, "quote"), str) and js_trim(entry["quote"])):
            continue
        clean.append(
            {
                "name": js_trim(entry["name"]),
                "place": _trimmed_or_null(prop(entry, "place")),
                "systemKw": _finite_number_or_null(prop(entry, "systemKw")),
                "installedOn": _trimmed_or_null(prop(entry, "installedOn")),
                "quote": js_trim(entry["quote"]),
                "billBefore": _finite_number_or_null(prop(entry, "billBefore")),
                "billAfter": _finite_number_or_null(prop(entry, "billAfter")),
                "monthlySaving": _finite_number_or_null(prop(entry, "monthlySaving")),
                "photoUri": _trimmed_or_null(prop(entry, "photoUri")),
            }
        )
        if len(clean) == 3:
            break
    if not clean:
        return blocked_section(
            dependency=Dependency.CMS.value,
            reference="§24",
            reason="No customer testimonials entered (data/quotation-testimonials.json → entries). Page 6 is omitted until real testimonials exist.",
        )
    video = prop(content, "video")
    video = video if js_truthy(video) and _is_js_object(video) else None
    video_value = None
    if video is not None and isinstance(prop(video, "title"), str) and js_trim(video["title"]):
        video_value = {
            "title": js_trim(video["title"]),
            "text": js_trim(video["text"]) if isinstance(prop(video, "text"), str) else None,
            "link": js_trim(video["link"]) if isinstance(prop(video, "link"), str) else None,
            "qrImageUri": js_trim(video["qrImageUri"]) if isinstance(prop(video, "qrImageUri"), str) else None,
        }
    intro = prop(content, "intro")
    return {
        "available": True,
        "source": "QUOTATION_CONTENT",
        "value": {"intro": js_trim(intro) if isinstance(intro, str) and js_trim(intro) else None, "entries": clean, "video": video_value},
    }


def _term_text(term: Any, part: str, lang: str) -> str:
    field = prop(term, part)
    return js_string(coalesce(prop(field, lang), prop(field, "en"), ""))


def _content_figures(content: Any, system_size_kw: Any) -> dict[str, Any]:
    """The figures the content pages print but must not compute: KSEB refund (80 % of ₹1,000 per kW) and where the
    terms split across pages 10/11 (balanced by text length, per language)."""
    kw = js_number(system_size_kw)
    positive = kw.is_finite() and kw > 0
    with localcontext(EXACT):
        refund = js_round(Decimal("0.8") * 1000 * kw) if positive else None
    split = {}
    terms = js_array(prop(content, "terms")) or []
    for lang in ("en", "ml"):
        weights = [120 + utf16_len(_term_text(term, "title", lang)) + utf16_len(_term_text(term, "body", lang)) for term in terms]
        total, running, cut = sum(weights), 0, len(terms)
        for index, weight in enumerate(weights):
            running += weight
            if running * 2 >= total:
                cut = index + 1
                break
        split[lang] = cut
    return {
        "systemSizeKw": kw if positive else None,
        "ksebRefundAmount": refund,
        "ksebRefundAmountText": format_en_in(refund) if refund is not None else None,
        "termsPage10Count": split,
    }


def _appliance_rows(content: Any, rows: Any, system_size_kw: Any) -> dict[str, Any] | None:
    """Sales-confirmed rows ``[{id, qty, hours}]`` (else the Project Head profile for the kW) priced in units/day."""
    if not js_truthy(content):
        return None
    profile_key = None
    rows = js_array(rows)
    if not rows:
        profiles = prop(prop(content, "appliances"), "profiles")
        profiles = profiles if js_truthy(profiles) else {}
        keys = sorted(key for key in (js_number(name) for name in js_keys(profiles)) if key.is_finite())
        size = js_number(system_size_kw)
        for key in keys:
            if not size.is_nan() and key <= size:
                profile_key = number_text(key)
        if profile_key is None and keys:
            profile_key = number_text(keys[0])
        rows = js_array(prop(profiles, profile_key)) if profile_key is not None else None
        if not rows:
            return None
    master: dict = {}
    for entry in js_array(prop(prop(content, "appliances"), "master")) or []:
        try:
            master[prop(entry, "id")] = entry
        except TypeError:
            continue
    out = []
    for row in rows:
        try:
            entry = master.get(prop(row, "id"))
        except TypeError:
            entry = None
        if entry is None:
            continue
        qty = _max_zero(prop(row, "qty"))
        hours = _max_zero(coalesce(prop(row, "hours"), prop(entry, "defaultHours")))
        watts = _max_zero(prop(entry, "watts"))
        if qty == 0:
            continue
        with localcontext(EXACT):
            units = js_round(qty * watts * hours / 1000 * 10) / 10
        icon = prop(entry, "icon")
        out.append({"id": prop(entry, "id"), "name": prop(entry, "name"), "icon": icon if js_truthy(icon) else "", "qty": qty, "hours": hours, "watts": watts, "units": units})
    if not out:
        return None
    with localcontext(EXACT):
        total = js_round(sum((row["units"] for row in out), Decimal(0)) * 10) / 10
    return {"rows": out, "total": total, "profileKey": profile_key}


def _max_zero(value: Any) -> Decimal:
    """``Math.max(0, Number(v) || 0)``."""
    number = js_number(value)
    if number.is_nan() or number <= 0:
        return Decimal(0)
    return number


# ---------------------------------------------------------------------------------------------------------------------
# The assembler
# ---------------------------------------------------------------------------------------------------------------------


def build_quotation_payload(
    *,
    quotation: Any = None,
    customer: Any = None,
    site: Any = None,
    system: Any = None,
    bom_snapshot: Any = None,
    commercial_snapshot: Any = None,
    engineering: Any = None,
    component_attributes: Any = None,
    upgrade_identity: Any = None,
    subsidy_treatment: Any = None,
    capabilities: Sequence[str] | None = None,
    variant: Any = "FULL",
    savings_result: Any = None,
    subsidy_result: Any = None,
    financing_result: Any = None,
    energy_profile_result: Any = None,
    campaign_result: Any = None,
    company_profile: Any = None,
    tier_display_names: Any = None,
    inclusion_matrix: Any = None,
    testimonials_content: Any = None,
    document_content: Any = None,
    appliance_rows: Any = None,
) -> Mapping:
    """``buildQuotationPayload``: the complete, deep-frozen document payload (``payloadVersion 'quotationPayload.1'``).

    ``capabilities`` containing ``COST_VIEW`` admits the internal cost and margin (the issued document is built with
    it; readers without it get :func:`project_issued_payload_for_actor`). The payload is built once and completely;
    ``variant`` only selects sections at render time.
    """
    include_internal = CAPABILITY_COST_VIEW in (capabilities or ())
    gate = evaluate_generation_gate(
        customer=customer,
        system=system,
        bom_snapshot=bom_snapshot,
        engineering=engineering,
        commercial_snapshot=commercial_snapshot,
        subsidy_treatment=subsidy_treatment,
        quotation=quotation,
    )

    frozen_content = None
    if js_truthy(document_content) and js_truthy(prop(document_content, "content")):
        frozen_content = {"version": _nullable(prop(document_content, "version")), "content": deep_freeze(document_content["content"])}
    size_kw = _nullable(prop(system, "systemSizeKw"))
    appliances = _appliance_rows(frozen_content["content"] if frozen_content else None, appliance_rows, size_kw)
    energy_available = js_truthy(energy_profile_result) and js_truthy(prop(energy_profile_result, "available"))
    generation_low = js_number(prop(energy_profile_result, "dailyGenerationLow")) if energy_available else Decimal("NaN")
    generation_high = js_number(prop(energy_profile_result, "dailyGenerationHigh")) if energy_available else Decimal("NaN")
    surplus = None
    if appliances and generation_low.is_finite() and generation_high.is_finite():
        with localcontext(EXACT):
            surplus = {
                "low": round_places(max(Decimal(0), generation_low - appliances["total"]), 1),
                "high": round_places(max(Decimal(0), generation_high - appliances["total"]), 1),
            }
    figures = _content_figures(frozen_content["content"], size_kw) if frozen_content else None

    bom_usable = js_truthy(bom_snapshot) and prop(bom_snapshot, "status") == _BOM_SNAPSHOT_LOCKED and isinstance(prop(bom_snapshot, "lines"), (list, tuple))
    rows = _build_rows(bom_snapshot, component_attributes or {}) if bom_usable else []
    customer_rows = [row for row in rows if row["customerFacing"]]
    if bom_usable:
        bom_summary = {
            "available": True,
            "source": Source.LOCKED_BOM_SNAPSHOT.value,
            "bomSnapshotId": _nullable(prop(bom_snapshot, "projectId")),
            "identitySource": Source.LOCKED_BOM_SNAPSHOT.value,
            "componentIds": [row["componentId"] for row in customer_rows],
            "rows": customer_rows,
            "internalOnlyLineCount": len(rows) - len(customer_rows),
            "internalOnlyNote": "Internal consumables and installation materials stay in the internal BOM (§16).",
        }
        technical = {
            "available": True,
            "source": Source.LOCKED_BOM_SNAPSHOT.value,
            "rows": customer_rows,
            "note": "Curated customer-facing equipment list (§16). Derived from the same rows as bomSummary.",
        }
    else:
        bom_summary = {
            "available": False,
            "source": None,
            "rows": [],
            "blocked": blocked_section(
                dependency=Dependency.LOCKED_BOM_SNAPSHOT.value, reference="§6", reason="No LOCKED BOM snapshot supplied. Component identity is never taken from anywhere else."
            ),
        }
        technical = {
            "available": False,
            "source": None,
            "rows": [],
            "blocked": blocked_section(dependency=Dependency.LOCKED_BOM_SNAPSHOT.value, reference="§16", reason="No LOCKED BOM snapshot supplied."),
        }

    pinned_versions = prop(commercial_snapshot, "versions") if js_truthy(commercial_snapshot) else UNDEFINED
    versions = {key: (_nullable(prop(pinned_versions, key)) if js_truthy(pinned_versions) else None) for key in REQUIRED_VERSION_KEYS}

    payload: dict[str, Any] = {
        "payloadVersion": QUOTATION_PAYLOAD_VERSION,
        "variant": variant,
        "variantNote": "The payload is built once and completely. `variant` selects sections at render time (C14); nothing is trimmed here.",
        "sectionNumberingNote": "Sections are named, never numbered. Page numbering is decision C13 and is PENDING.",
        "generationGate": gate.as_dict(),
        "quotation": (
            {
                "quotationNumber": _nullable(prop(quotation, "quotationNumber")),
                "quotationVersion": _nullable(prop(quotation, "quotationVersion")),
                "quotationDate": _nullable(prop(quotation, "quotationDate")),
                "validUntil": _nullable(prop(quotation, "validUntil")),
                "proposalBy": _nullable(prop(quotation, "proposalBy")),
                "salespersonId": _nullable(prop(quotation, "salespersonId")),
                "quotationLanguage": _nullable(prop(quotation, "quotationLanguage")),
                "status": _nullable(prop(quotation, "status")),
                "quotationSource": _nullable(prop(quotation, "quotationSource")),
                "district": _nullable(prop(quotation, "district")),
                "affiliateId": _nullable(prop(quotation, "affiliateId")),
                "identityModel": IDENTITY_MODEL,
                "identityNote": "P3-D is unresolved: proposalBy / salespersonId are asserted, not authenticated.",
                "source": Source.QUOTATION_RECORD.value,
            }
            if js_truthy(quotation)
            else blocked_section(dependency=Dependency.LOCKED_BOM_SNAPSHOT.value, reference="§4", reason="No quotation record supplied.")
        ),
        "customer": (
            {**_spread(customer), "source": Source.CUSTOMER_RECORD.value}
            if js_truthy(customer)
            else blocked_section(dependency=Dependency.COMMERCIAL_SNAPSHOT.value, reference="§3", reason="No customer record supplied.")
        ),
        "site": {**_spread(site), "source": Source.PROJECT_CONFIGURATION.value} if js_truthy(site) else None,
        "system": (
            {
                **_spread(system),
                "packageTierDisplayName": _tier_display_name(prop(system, "packageTier"), tier_display_names),
                "recommendedBadge": _recommended_badge(prop(system, "packageTier"), tier_display_names),
                "tierColumnNames": {tier: _tier_display_name(tier, tier_display_names) for tier in _TIERS},
                "source": Source.PROJECT_CONFIGURATION.value,
            }
            if js_truthy(system)
            else blocked_section(dependency=Dependency.LOCKED_BOM_SNAPSHOT.value, reference="§5", reason="No system configuration supplied.")
        ),
        "inclusions": _inclusion_matrix(prop(system, "packageTier"), inclusion_matrix),
        "inclusionsByTier": {tier: _inclusion_matrix(tier, inclusion_matrix) for tier in _TIERS},
        "upgrade": (
            {**_spread(upgrade_identity), "source": Source.UPGRADE_MODEL.value, "reference": "C24 — upgrade identity lives on the quotation, not in the BOM."} if js_truthy(upgrade_identity) else None
        ),
        "engineering": (
            {
                "status": _nullable(prop(engineering, "status")),
                "blocked": list(js_array(prop(engineering, "blocked")) or []) if js_truthy(prop(engineering, "blocked")) else [],
                "warnings": list(js_array(prop(engineering, "warnings")) or []) if js_truthy(prop(engineering, "warnings")) else [],
                "acknowledgements": list(js_array(prop(engineering, "acknowledgements")) or []) if js_truthy(prop(engineering, "acknowledgements")) else [],
                "source": Source.ENGINEERING_CHECKER.value,
                "customerVisible": False,
                "note": "Internal engineering warnings are not customer-facing (§30). Carried for the gate and for internal review.",
            }
            if js_truthy(engineering)
            else blocked_section(dependency=Dependency.LOCKED_BOM_SNAPSHOT.value, reference="§28", reason="No engineering verdict supplied.")
        ),
        "bomSummary": bom_summary,
        "technicalSpecifications": technical,
        "pricing": _build_pricing(commercial_snapshot, include_internal),
        "extras": _build_extras(commercial_snapshot),
        "subsidy": _build_subsidy_section(subsidy_treatment, subsidy_result),
        "savings": _engine_section(savings_result, "§21", Dependency.SAVINGS_ENGINE, "No Savings Engine. Generation, savings, payback and long-term figures have no approved formula."),
        "financing": _engine_section(
            financing_result, "§22", Dependency.FINANCE_ENGINE, "No Finance Engine. The loan-amount basis is explicitly undecided — price after subsidy is not the loan amount."
        ),
        "consumption": _consumption(energy_profile_result, energy_available),
        "content": (
            {
                "available": True,
                "source": "QUOTATION_CONTENT_STORE",
                "status": "OK",
                "value": {**frozen_content, "derived": figures},
                "dependency": None,
                "reference": "P1",
                "reason": None,
            }
            if frozen_content
            else blocked_section(
                dependency="QUOTATION_CONTENT_STORE",
                reference="P1",
                reason="No published quotation content (data/quotation-content.json). Pages 3/9/10/11 print without editable text.",
            )
        ),
        "applianceUsage": _appliance_usage(appliances, surplus, energy_profile_result, energy_available),
        "energyProfile": _energy_profile(energy_profile_result, energy_available),
        "testimonials": _testimonials(testimonials_content),
        "campaign": _campaign(campaign_result),
        "terms": blocked_section(dependency=Dependency.CMS.value, reference="§30", reason="Terms are CMS content. No CMS exists in this build."),
        "company": (
            company_profile
            if js_truthy(company_profile)
            else blocked_section(
                dependency=Dependency.COMPANY_MASTER.value,
                reference="§30",
                reason="No Company Master configured. Admin must set up the company profile before company identity appears on the quotation.",
            )
        ),
        "versions": versions,
        "versionsSource": Source.COMMERCIAL_SNAPSHOT.value,
        "versionKeys": list(REQUIRED_VERSION_KEYS),
    }
    return deep_freeze(payload)


def _engine_section(result: Any, reference: str, dependency: Dependency, reason: str) -> dict[str, Any]:
    if js_truthy(result) and js_truthy(prop(result, "available")):
        return {
            "available": True,
            "source": _nullable(prop(result, "source")),
            "status": _nullable(prop(result, "status")),
            "value": _spread(result),
            "dependency": None,
            "reference": reference,
            "reason": None,
        }
    return blocked_section(dependency=dependency.value, reference=reference, reason=reason)


def _consumption(result: Any, available: bool) -> dict[str, Any]:
    if not available:
        return blocked_section(
            dependency=Dependency.ENERGY_LOAD_CALCULATION_ENGINE.value,
            reference="§23",
            reason="No Energy/Load Calculation Engine. Consumption and recommended size are not estimated here.",
        )
    return {
        "available": True,
        "source": _nullable(prop(result, "source")),
        "status": _nullable(prop(result, "status")),
        "value": {
            "monthlyConsumption": _nullable(prop(result, "monthlyConsumption")),
            "monthlyConsumptionUnit": coalesce(prop(result, "monthlyConsumptionUnit"), "kWh"),
            "annualConsumption": _nullable(prop(result, "annualConsumption")),
            "annualConsumptionUnit": coalesce(prop(result, "annualConsumptionUnit"), "kWh"),
            "recommendedSystemSizeKw": _nullable(prop(result, "recommendedSystemSizeKw")),
            "exactSystemSizeKw": _nullable(prop(result, "exactSystemSizeKw")),
            "averageTariffRate": _nullable(prop(result, "averageTariffRate")),
            "averageTariffRateUnit": coalesce(prop(result, "averageTariffRateUnit"), "₹/kWh"),
            "regionId": _nullable(prop(result, "regionId")),
            "regionName": _nullable(prop(result, "regionName")),
            "tariffName": _nullable(prop(result, "tariffName")),
            "tariffVersion": _nullable(prop(result, "tariffVersion")),
        },
        "dependency": None,
        "reference": "§23",
        "reason": None,
    }


def _appliance_usage(appliances: dict | None, surplus: dict | None, result: Any, available: bool) -> dict[str, Any]:
    if appliances:
        return {
            "available": True,
            "source": "SALES_APPLIANCE_ROWS",
            "status": "OK",
            "value": {
                "appliances": appliances["rows"],
                "totalDailyUsage": appliances["total"],
                "profileKey": _nullable(appliances["profileKey"]),
                "surplusExportedLow": surplus["low"] if surplus else None,
                "surplusExportedHigh": surplus["high"] if surplus else None,
            },
            "dependency": None,
            "reference": "§23",
            "reason": None,
        }
    if available:
        return {
            "available": True,
            "source": _nullable(prop(result, "source")),
            "status": _nullable(prop(result, "status")),
            "value": {"appliances": coalesce(prop(result, "appliances"), []), "totalDailyUsage": _nullable(prop(result, "totalDailyUsage"))},
            "dependency": None,
            "reference": "§23",
            "reason": None,
        }
    return blocked_section(
        dependency=Dependency.ENERGY_LOAD_CALCULATION_ENGINE.value,
        reference="§23",
        reason="No Energy/Load Calculation Engine. Appliance daily units have no approved formula.",
    )


def _energy_profile(result: Any, available: bool) -> dict[str, Any]:
    if not available:
        return blocked_section(
            dependency=Dependency.ENERGY_LOAD_CALCULATION_ENGINE.value,
            reference="§23",
            reason="No Energy/Load Calculation Engine. Generation profile has no approved formula.",
        )
    return {
        "available": True,
        "source": _nullable(prop(result, "source")),
        "status": _nullable(prop(result, "status")),
        "value": {
            "dailyGenerationLow": prop(result, "dailyGenerationLow"),
            "dailyGenerationHigh": prop(result, "dailyGenerationHigh"),
            "monthlyKsebValueLow": prop(result, "monthlyKsebValueLow"),
            "monthlyKsebValueHigh": prop(result, "monthlyKsebValueHigh"),
            "homeUsesUnitsPerDay": _nullable(prop(result, "homeUsesUnitsPerDay")),
            "surplusExportedLow": _nullable(prop(result, "surplusExportedLow")),
            "surplusExportedHigh": _nullable(prop(result, "surplusExportedHigh")),
        },
        "dependency": None,
        "reference": "§23",
        "reason": None,
    }


def _campaign(result: Any) -> dict[str, Any]:
    if not (js_truthy(result) and js_truthy(prop(result, "available"))):
        return blocked_section(
            dependency=Dependency.CMS.value,
            reference="§25",
            reason="Campaign content is CMS/configuration. A monetary campaign benefit would have to be on the commercial snapshot.",
        )

    def spread_or(name: str, default: Any) -> Any:
        value = prop(result, name)
        return _spread(value) if js_truthy(value) else default

    offers = prop(result, "offers")
    return {
        "available": True,
        "source": coalesce(prop(result, "source"), "CMS"),
        "value": {
            "id": _nullable(prop(result, "id")),
            "name": _nullable(prop(result, "name")),
            "active": coalesce(prop(result, "active"), False),
            "template": _nullable(prop(result, "template")),
            "yearBadge": _nullable(prop(result, "yearBadge")),
            "theme": spread_or("theme", None),
            "headline": _nullable(prop(result, "headline")),
            "subheadline": _nullable(prop(result, "subheadline")),
            "heroImage": _nullable(prop(result, "heroImage")),
            "primaryBenefit": spread_or("primaryBenefit", None),
            "offers": [_spread(offer) for offer in offers] if isinstance(offers, (list, tuple)) else [],
            "luckyDraw": spread_or("luckyDraw", {"enabled": False}),
            "referral": spread_or("referral", {"enabled": False}),
            "urgency": spread_or("urgency", {"enabled": False}),
            "cta": spread_or("cta", {"enabled": False}),
            "artwork": spread_or("artwork", None),
            "disclaimer": _nullable(prop(result, "disclaimer")),
            "period": spread_or("period", None),
        },
        "reference": "§25",
    }


_PAYLOAD_ARGUMENTS = {
    "quotation": "quotation",
    "customer": "customer",
    "site": "site",
    "system": "system",
    "bomSnapshot": "bom_snapshot",
    "commercialSnapshot": "commercial_snapshot",
    "engineering": "engineering",
    "componentAttributes": "component_attributes",
    "upgradeIdentity": "upgrade_identity",
    "subsidyTreatment": "subsidy_treatment",
    "capabilities": "capabilities",
    "variant": "variant",
    "savingsResult": "savings_result",
    "subsidyResult": "subsidy_result",
    "financingResult": "financing_result",
    "energyProfileResult": "energy_profile_result",
    "campaignResult": "campaign_result",
    "companyProfile": "company_profile",
    "tierDisplayNames": "tier_display_names",
    "inclusionMatrix": "inclusion_matrix",
    "testimonialsContent": "testimonials_content",
    "documentContent": "document_content",
    "applianceRows": "appliance_rows",
}


def build_quotation_payload_from(inputs: Mapping[str, Any]) -> Mapping:
    """:func:`build_quotation_payload` from the JavaScript argument object (camelCase keys; unknown keys ignored)."""
    return build_quotation_payload(**{name: inputs[key] for key, name in _PAYLOAD_ARGUMENTS.items() if key in inputs and inputs[key] is not UNDEFINED})


# ---------------------------------------------------------------------------------------------------------------------
# quotationWorkspace.js — pure assembly around the payload
# ---------------------------------------------------------------------------------------------------------------------

_INVERTER_DEVICE_LABEL: Mapping[str, str] = FrozenDict(
    string_inverter="String Inverter", micro_inverter="Micro Inverter", microinverter="Micro Inverter", hybrid_inverter="Hybrid Inverter", hybrid="Hybrid Inverter"
)


def component_attributes_for(catalog: Any, component_ids: Sequence[Any] = ()) -> Mapping:
    """``componentAttributesFor``: the whitelisted display attributes of the ids the locked BOM chose (never a price;
    an id absent from the catalogue is simply not decorated)."""
    wanted = {component_id for component_id in component_ids if js_truthy(component_id) and isinstance(component_id, str)}
    out: dict[str, Any] = {}
    categories = prop(catalog, "categories")
    for key in js_keys(categories):
        for item in js_array(prop(categories[key], "items")) or []:
            component_id = prop(item, "id")
            if not isinstance(component_id, str) or component_id not in wanted:
                continue
            attributes: dict[str, Any] = {}
            if not is_nullish(prop(item, "brand")):
                attributes["brand"] = item["brand"]
            if not is_nullish(prop(item, "model")):
                attributes["model"] = item["model"]
            elif not is_nullish(prop(item, "name")):
                attributes["model"] = item["name"]
            if not is_nullish(prop(item, "watt")):
                attributes["moduleWatt"] = item["watt"]
            if not is_nullish(prop(item, "kw")):
                attributes["capacity"] = f"{js_string(item['kw'])} kW"
            if not is_nullish(prop(item, "phase")):
                attributes["phase"] = item["phase"]
            device = prop(item, "deviceType")
            label = _INVERTER_DEVICE_LABEL.get(device) if isinstance(device, str) else None
            if label:
                attributes["technology"] = label
            elif not is_nullish(prop(item, "type")):
                attributes["technology"] = item["type"]
            elif not is_nullish(prop(item, "panelType")):
                attributes["technology"] = item["panelType"]
            if not is_nullish(prop(item, "dcr")):
                attributes["dcr"] = item["dcr"]
            if not is_nullish(prop(item, "sqmm")):
                attributes["crossSection"] = item["sqmm"]
            if not is_nullish(prop(item, "unit")):
                attributes["unit"] = item["unit"]
            if isinstance(prop(item, "specification"), str) and js_trim(item["specification"]):
                attributes["specification"] = js_trim(item["specification"])
            if isinstance(prop(item, "warranty"), str) and js_trim(item["warranty"]):
                attributes["warranty"] = js_trim(item["warranty"])
            out[component_id] = attributes
    return deep_freeze(out)


_DCR_TECHNOLOGY = re.compile(r"(DCR|NON[_-]?DCR)", re.IGNORECASE | re.ASCII)


def resolve_panel_dcr_type(bom_snapshot: Any, component_attributes: Any = None) -> str | None:
    """``resolvePanelDcrType``: ``'DCR'``/``'NON_DCR'`` from the locked BOM's panel line(s), or ``None`` when any panel's
    status is unknown (the subsidy engine is then not called). Mixed panels resolve to ``'NON_DCR'``."""
    lines = js_array(prop(bom_snapshot, "lines")) or []
    panels = [line for line in lines if js_truthy(line) and (prop(line, "role") == "PANEL" or prop(line, "category") in ("panel", "module", "solar_panel"))]
    if not panels:
        return None
    types = []
    for line in panels:
        found = None
        if not is_nullish(prop(line, "panelType")):
            found = js_string(line["panelType"]).upper()
        elif not is_nullish(prop(line, "dcr")):
            found = "DCR" if js_truthy(line["dcr"]) else "NON_DCR"
        else:
            attributes = _attribute_lookup(component_attributes, prop(line, "componentId")) if js_truthy(component_attributes) and js_truthy(prop(line, "componentId")) else None
            if js_truthy(attributes) and not is_nullish(prop(attributes, "dcr")):
                found = "DCR" if js_truthy(attributes["dcr"]) else "NON_DCR"
            elif js_truthy(attributes) and isinstance(prop(attributes, "technology"), str) and _DCR_TECHNOLOGY.fullmatch(attributes["technology"]):
                found = attributes["technology"].upper().replace("-", "_", 1)
        if found is None:
            return None
        types.append("DCR" if found == "DCR" else "NON_DCR")
    unique = list(dict.fromkeys(types))
    return unique[0] if len(unique) == 1 else "NON_DCR"


def panel_unresolved_subsidy_result(subsidy_type: Any) -> Mapping:
    """The subsidy result carried when the locked BOM has no resolvable panel DCR status (engine not called)."""
    return deep_freeze(
        {
            "source": "SUBSIDY_ENGINE",
            "status": "INVALID_INPUT",
            "available": False,
            "reason": "Panel DCR status could not be resolved from the locked BOM / catalog. Subsidy eligibility cannot be stated.",
            "engineVersion": None,
            "eligibilityStatus": None,
            "subsidyType": subsidy_type,
            "totalSubsidy": None,
            "calculatedAt": None,
        }
    )


def alternative_inputs(record: Mapping, option: Mapping, version: Any, *, bom_snapshot: Any = None, commercial_snapshot: Any = None) -> Mapping:
    """The payload inputs of one alternative tier (``buildAlternativePayloads``): the record's system merged with the
    option's, the option's pinned snapshots, and a quotation section without source/district/affiliate."""
    system = {**_spread(coalesce(prop(record, "system"), {})), **_spread(coalesce(prop(option, "system"), {}))}
    system["packageTier"] = coalesce(prop(option, "tier"), prop(option, "packageTier"), prop(prop(record, "system"), "packageTier"), None)
    quotation = {
        "quotationNumber": _nullable(prop(record, "quotationNumber")),
        "quotationVersion": version,
        "quotationDate": _nullable(prop(record, "quotationDate")),
        "validUntil": _nullable(prop(record, "validUntil")),
        "proposalBy": _nullable(prop(record, "proposalBy")),
        "salespersonId": _nullable(prop(record, "salespersonId")),
        "quotationLanguage": _nullable(prop(record, "quotationLanguage")),
        "status": prop(record, "status"),
    }
    return deep_freeze(
        {
            "quotation": quotation,
            "customer": _nullable(prop(record, "customer")),
            "site": _nullable(prop(record, "site")),
            "system": system,
            "bomSnapshot": bom_snapshot if js_truthy(prop(option, "bomSnapshotId")) else None,
            "commercialSnapshot": commercial_snapshot if js_truthy(prop(option, "commercialSnapshotId")) else None,
            "engineering": _nullable(prop(record, "engineering")),
            "upgradeIdentity": _nullable(prop(record, "upgradeIdentity")),
            "subsidyTreatment": _nullable(prop(record, "subsidyTreatment")),
            "variant": coalesce(prop(record, "variant"), "FULL"),
        }
    )


def alternative_entry(index: int, option: Mapping, system: Mapping, payload: Mapping) -> Mapping:
    """``{optionIndex, tier, bomSnapshotId, commercialSnapshotId, payload}``."""
    return deep_freeze(
        {
            "optionIndex": index,
            "tier": prop(system, "packageTier"),
            "bomSnapshotId": _nullable(prop(option, "bomSnapshotId")),
            "commercialSnapshotId": _nullable(prop(option, "commercialSnapshotId")),
            "payload": payload,
        }
    )


def with_alternatives(payload: Mapping, alternatives: Sequence[Mapping]) -> Mapping:
    """``{...payload, alternatives}`` when there are alternatives, else the payload unchanged."""
    return deep_freeze({**payload, "alternatives": list(alternatives)}) if alternatives else payload


_WITHHELD_REASON = "Internal cost and margin require COST_VIEW (§17, §30). They are absent from this payload, not masked."


def project_issued_payload_for_actor(payload: Any, can_see_internal_cost: bool) -> Any:
    """``projectIssuedPayloadForActor``: an ISSUED payload for a reader without COST_VIEW — ``pricing.internal`` and the
    internal version labels withheld on the primary and every alternative payload. The stored payload is untouched."""
    if not js_truthy(payload) or can_see_internal_cost:
        return payload

    def withhold(part: Any) -> Any:
        if not js_truthy(part) or not _is_js_object(part):
            return part
        out = _spread(part)
        pricing = out.get("pricing")
        if js_truthy(pricing) and _is_js_object(pricing) and "internal" in _spread(pricing):
            out["pricing"] = {**_spread(pricing), "internal": None, "internalWithheld": True, "internalWithheldReason": _WITHHELD_REASON}
        versions = out.get("versions")
        if js_truthy(versions) and _is_js_object(versions):
            out["versions"] = {key: value for key, value in _spread(versions).items() if key not in _INTERNAL_VERSION_KEYS}
        if isinstance(out.get("versionKeys"), (list, tuple)):
            out["versionKeys"] = [key for key in out["versionKeys"] if key not in _INTERNAL_VERSION_KEYS]
        return out

    primary = withhold(payload)
    alternatives = prop(payload, "alternatives")
    if isinstance(alternatives, (list, tuple)):
        primary = {
            **primary,
            "alternatives": [
                {**_spread(alternative), "payload": withhold(alternative["payload"])} if js_truthy(alternative) and js_truthy(prop(alternative, "payload")) else withhold(alternative)
                for alternative in alternatives
            ],
        }
    return deep_freeze(primary)


_SALES_VISIBLE_SNAPSHOT_DOMAINS = ("schemaVersion", "frozenAt", "validity", "subsidy", "branding", "rendererPin")


def project_issued_snapshot_for_actor(snapshot: Any, can_see_internal_cost: bool) -> Any:
    """``projectIssuedSnapshotForActor``: only the customer-facing freeze domains for a reader without COST_VIEW."""
    if not js_truthy(snapshot) or not _is_js_object(snapshot):
        return _nullable(snapshot)
    if can_see_internal_cost:
        return snapshot
    members = _spread(snapshot)
    out = {key: members[key] for key in _SALES_VISIBLE_SNAPSHOT_DOMAINS if key in members}
    out["internalDomainsWithheld"] = True
    return deep_freeze(out)


def verify_immutability(document: Mapping, commercial_snapshot: Any) -> Mapping:
    """``verifyImmutability``: does the stored document's customer price still equal its commercial snapshot?"""
    customer = prop(prop(prop(document, "payload"), "pricing"), "customer")
    pricing = prop(commercial_snapshot, "pricing")
    keys = ("sellingPriceBeforeGST", "gstAmount", "sellingPriceIncludingGST", "customerTotalIncludingGST")
    matches = js_truthy(commercial_snapshot) and js_truthy(customer) and all(_strict_equal(prop(customer, key), prop(pricing, key)) for key in keys)
    return deep_freeze(
        {
            "quotationId": prop(document, "quotationId"),
            "version": prop(document, "version"),
            "frozen": is_frozen(document),
            "commercialSnapshotId": prop(document, "commercialSnapshotId"),
            "snapshotPresent": js_truthy(commercial_snapshot),
            "matchesSnapshot": bool(matches),
        }
    )


def _strict_equal(left: Any, right: Any) -> bool:
    if is_number(left) and is_number(right):
        return left == right
    if left is UNDEFINED or right is UNDEFINED:
        return left is right
    return type(left) is type(right) and left == right


class RendererPinningInvalid(ValueError):
    """``MISSING_IDENTIFIER``: a renderer pin that is not an object of non-empty strings."""

    code = "MISSING_IDENTIFIER"

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


_PIN_FIELDS = ("name", "version", "bundleSha256", "templateSha256", "manifestSha256", "rendererVersion")


def normalize_renderer_pinning(pinning: Any, at: Any) -> Mapping | None:
    """``normalizeRendererPinning``: the approved-template pin frozen into the document (``pinnedAt`` defaults to ``at``)."""
    if is_nullish(pinning):
        return None
    if not _is_js_object(pinning):
        raise RendererPinningInvalid("rendererPinning must be an object with { name, version, bundleSha256, templateSha256, rendererVersion } string fields.", {"got": type(pinning).__name__})
    out: dict[str, Any] = {}
    for key in _PIN_FIELDS:
        value = prop(pinning, key)
        if is_nullish(value):
            continue
        if not isinstance(value, str) or not value:
            raise RendererPinningInvalid(f"rendererPinning.{key} must be a non-empty string.", {"field": key, "got": type(value).__name__})
        out[key] = value
    pinned_at = prop(pinning, "pinnedAt")
    out["pinnedAt"] = pinned_at if isinstance(pinned_at, str) and pinned_at else at
    return deep_freeze(out)


def issued_document(
    *,
    quotation_id: Any,
    version: int,
    issued_by: Any,
    issued_by_role: Any,
    issued_at: Any,
    commercial_snapshot_id: Any,
    bom_snapshot_id: Any,
    cms_page_versions: Any = None,
    renderer_pinning: Any = None,
    payload: Mapping,
    snapshot: Any = None,
) -> Mapping:
    """The deep-frozen ISSUED document (``issueQuotation``): identity, pins, the complete payload and the freeze."""
    return deep_freeze(
        {
            "quotationId": quotation_id,
            "version": version,
            "status": VersionStatus.ISSUED.value,
            "issuedBy": issued_by,
            "issuedByRole": issued_by_role,
            "issuedAt": issued_at,
            "identityModel": IDENTITY_MODEL,
            "commercialSnapshotId": commercial_snapshot_id,
            "bomSnapshotId": bom_snapshot_id,
            "cmsPageVersions": cms_page_versions,
            "rendererPinning": renderer_pinning,
            "payload": payload,
            "snapshot": snapshot,
        }
    )


# ---------------------------------------------------------------------------------------------------------------------
# commercialSnapshot.js — every version that can move a number, pinned; never repriced
# ---------------------------------------------------------------------------------------------------------------------


class CommercialSnapshotStatus(StrEnum):
    ISSUED = "ISSUED"
    SUPERSEDED = "SUPERSEDED"


class SnapshotError(ValueError):
    """A commercial snapshot refused from an incomplete (cost), rejected (pricing) or blocked (pack) result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _offer_section(offer: Any) -> dict[str, Any] | None:
    if not js_truthy(offer):
        return None
    value = prop(offer, "offerValue")
    if is_nullish(value):
        raw = prop(offer, "value")
        value = js_number(raw) if not is_nullish(raw) else None
        value = value if value is None or value.is_finite() else None
    status = prop(offer, "offerStatus")
    return {
        "offerId": coalesce(prop(offer, "offerId"), prop(offer, "id"), None),
        "offerName": coalesce(prop(offer, "offerName"), prop(offer, "name"), None),
        "offerType": coalesce(prop(offer, "offerType"), prop(offer, "type"), None),
        "offerValue": value,
        "offerAmount": _nullable(prop(offer, "offerAmount")),
        "offerVersion": coalesce(prop(offer, "offerVersion"), prop(offer, "version"), None),
        "offerStatus": status if not is_nullish(status) else ("ACTIVE" if js_truthy(prop(offer, "active")) else None),
    }


def _offer_version(offer: Any) -> Any:
    return coalesce(prop(offer, "offerVersion"), prop(offer, "version"), None) if js_truthy(offer) else None


def create_commercial_snapshot(
    *,
    snapshot_id: Any = UNDEFINED,
    cost_result: Any = None,
    pricing_result: Any = None,
    issued_by: Any = None,
    issued_at: Any = None,
    money_rule_version: str = "money.1",
    offer: Any = None,
) -> Mapping:
    """``createCommercialSnapshot`` (gross-margin mode): an ISSUED snapshot of a COMPLETE cost and pricing result."""
    if not js_truthy(cost_result) or prop(cost_result, "status") != "COMPLETE":
        raise SnapshotError("COST_INCOMPLETE", "cannot issue a commercial snapshot from an incomplete cost result")
    if not js_truthy(pricing_result) or prop(pricing_result, "status") != "COMPLETE":
        raise SnapshotError("PRICING_REJECTED", "cannot issue a commercial snapshot from a rejected pricing result")
    cost, pricing = cost_result, pricing_result
    material = coalesce(prop(cost, "materialLandedCost"), prop(cost, "materialCost"))
    total = coalesce(prop(cost, "totalActualProjectCost"), prop(cost, "totalActualCost"))
    return deep_freeze(
        {
            "snapshotId": snapshot_id,
            "status": CommercialSnapshotStatus.ISSUED.value,
            "projectId": prop(cost, "projectId"),
            "issuedBy": issued_by,
            "issuedAt": issued_at,
            "versions": {
                "bomSnapshotId": prop(cost, "bomSnapshotId"),
                "catalogVersion": prop(cost, "catalogVersion"),
                "procurementPriceVersion": prop(cost, "procurementPriceVersion"),
                "landedCostVersion": _nullable(prop(cost, "landedCostVersion")),
                "projectRateCardVersion": _nullable(prop(cost, "projectRateCardVersion")),
                "costConfigVersion": prop(cost, "costConfigVersion"),
                "marginVersion": prop(pricing, "marginVersion"),
                "gstVersion": prop(pricing, "gstVersion"),
                "costEngineVersion": prop(cost, "costEngineVersion"),
                "pricingEngineVersion": prop(pricing, "pricingEngineVersion"),
                "moneyRuleVersion": money_rule_version,
                "offerVersion": _offer_version(offer),
                "marketRateVersion": _nullable(prop(prop(pricing, "marketRateReference"), "value")),
            },
            "cost": {
                "materialLandedCost": material,
                "materialCost": material,
                "structureCost": prop(cost, "structureCost"),
                "installationCost": prop(cost, "installationCost"),
                "siteSurveyCost": prop(cost, "siteSurveyCost"),
                "engineeringDesignCost": prop(cost, "engineeringDesignCost"),
                "transportationCost": prop(cost, "transportationCost"),
                "serviceAmcCost": prop(cost, "serviceAmcCost"),
                "miscellaneousCost": prop(cost, "miscellaneousCost"),
                "officeExpenseAllocation": prop(cost, "officeExpenseAllocation"),
                "specialProjectWorksCost": _nullable(prop(cost, "specialProjectWorksCost")),
                "directProjectCost": _nullable(prop(cost, "directProjectCost")),
                "totalActualProjectCost": total,
                "totalActualCost": total,
                "traces": prop(cost, "traces"),
            },
            "pricing": {
                "marginType": prop(pricing, "marginType"),
                "targetGrossMargin": prop(pricing, "targetGrossMargin"),
                "sellingPriceBeforeGST": prop(pricing, "sellingPriceBeforeGST"),
                "grossProfit": prop(pricing, "grossProfit"),
                "gstRatePct": prop(pricing, "gstRatePct"),
                "gstAmount": prop(pricing, "gstAmount"),
                "sellingPriceIncludingGST": prop(pricing, "sellingPriceIncludingGST"),
                "extras": prop(pricing, "extras"),
                "extrasIncludingGST": prop(pricing, "extrasIncludingGST"),
                "customerTotalIncludingGST": prop(pricing, "customerTotalIncludingGST"),
                "customerSideExpenses": coalesce(prop(pricing, "customerSideExpenses"), []),
            },
            "offer": _offer_section(offer),
        }
    )


def _add(*values: Any) -> Any:
    """JavaScript ``a + b + c`` on numbers (a null counts 0, undefined makes NaN → null)."""
    with localcontext(EXACT):
        total = sum((js_number(value) for value in values), Decimal(0))
    return total if total.is_finite() else None


def create_pack_commercial_snapshot(
    *,
    snapshot_id: Any = UNDEFINED,
    project_id: Any = UNDEFINED,
    bom_snapshot_id: Any = UNDEFINED,
    pack_pricing: Any = None,
    config_version: Any = UNDEFINED,
    material_list: Sequence[Any] | None = (),
    cost_result: Any = None,
    issued_by: Any = None,
    issued_at: Any = None,
    money_rule_version: str = "money.1",
    offer: Any = None,
    catalog_version: Any = None,
) -> Mapping:
    """``createPackCommercialSnapshot``: the ISSUED snapshot of the owner's pack formula (market rate + swap deltas +
    roof add-on, transport beyond the included km as a customer extra), same shape as the gross-margin snapshot."""
    if not js_truthy(pack_pricing) or prop(pack_pricing, "status") != "COMPLETE":
        raise SnapshotError("PACK_PRICING_BLOCKED", "cannot issue a pack commercial snapshot from a blocked pack pricing result")
    customer = prop(pack_pricing, "customer")
    inputs = prop(pack_pricing, "inputs")
    detail = prop(customer, "transportExtraDetail")
    landed_ok = js_truthy(cost_result) and prop(cost_result, "status") == "COMPLETE"
    config = f"pack-config@v{js_string(config_version)}"
    transport_extra = prop(customer, "transportExtra")
    extras = []
    if _positive(transport_extra):
        extras.append(
            {
                "extraId": "TRANSPORT_EXTRA_KM",
                "type": "TRANSPORT",
                "description": (
                    f"Transport beyond {js_string(prop(detail, 'includedKm'))} km included: {js_string(prop(detail, 'extraKm'))} km × "
                    f"₹{js_string(prop(detail, 'ratePerKm'))}/km ({js_string(prop(detail, 'vehicleType'))})"
                ),
                "includedInBase": False,
                "semantic": "CUSTOMER_EXTRA",
                "customerPriceExcludingGST": transport_extra,
                "gstRegime": "NOT_APPLIED",
                "gstRatePct": 0,
                "gstAmount": 0,
                "customerPriceIncludingGST": transport_extra,
                "detail": _spread(detail),
            }
        )
    trace = {
        "head": "TRANSPORTATION",
        "amount": transport_extra,
        "inputs": {
            "vehicleType": prop(detail, "vehicleType"),
            "distanceKm": prop(detail, "distanceKm"),
            "includedKm": prop(detail, "includedKm"),
            "extraKm": prop(detail, "extraKm"),
            "ratePerKm": prop(detail, "ratePerKm"),
            "distanceBasis": "ONE_WAY",
        },
        "formula": "max(0, distanceKm − includedKm) × ratePerKm — included km are inside the pack market rate",
        "version": config,
        "source": "PACK_CONFIG_TRANSPORT",
    }
    internal = prop(pack_pricing, "internal")
    reference = prop(internal, "referenceCost")
    gst = prop(pack_pricing, "gst")
    if landed_ok:
        landed_check = {
            "status": prop(cost_result, "status"),
            "totalActualProjectCost": coalesce(prop(cost_result, "totalActualProjectCost"), prop(cost_result, "totalActualCost"), None),
            "landedCostVersion": _nullable(prop(cost_result, "landedCostVersion")),
        }
    else:
        errors = js_array(prop(cost_result, "errors")) if js_truthy(cost_result) else None
        landed_check = {"status": coalesce(prop(cost_result, "status"), "NOT_RUN"), "errors": [prop(error, "code") for error in errors or []]}
    return deep_freeze(
        {
            "snapshotId": snapshot_id,
            "status": CommercialSnapshotStatus.ISSUED.value,
            "projectId": project_id,
            "issuedBy": issued_by,
            "issuedAt": issued_at,
            "pricingMode": "PACK_MARKET_RATE",
            "gstRegime": prop(gst, "regime"),
            "versions": {
                "bomSnapshotId": bom_snapshot_id,
                "catalogVersion": coalesce(catalog_version, prop(cost_result, "catalogVersion") if js_truthy(cost_result) else None, None),
                "procurementPriceVersion": prop(cost_result, "procurementPriceVersion") if landed_ok else None,
                "landedCostVersion": _nullable(prop(cost_result, "landedCostVersion")) if landed_ok else None,
                "projectRateCardVersion": None,
                "costConfigVersion": config,
                "marginVersion": f"pack-market-rate@v{js_string(config_version)}",
                "gstVersion": f"{js_string(prop(gst, 'regime'))}@{js_string(prop(gst, 'ratePct'))}",
                "costEngineVersion": prop(cost_result, "costEngineVersion") if landed_ok else None,
                "pricingEngineVersion": prop(pack_pricing, "packPricingVersion"),
                "moneyRuleVersion": money_rule_version,
                "offerVersion": _offer_version(offer),
                "marketRateVersion": f"{config}:{js_string(prop(pack_pricing, 'marketRateKey'))}/{js_string(prop(inputs, 'size'))}",
            },
            "cost": {
                "basis": "PH_REFERENCE_COST",
                "note": "Catalog reference prices + PH installation matrix + charges. Not procurement landed cost. See landedCostCheck.",
                "materialLandedCost": coalesce(prop(cost_result, "materialLandedCost"), prop(cost_result, "materialCost")) if landed_ok else None,
                "materialCost": prop(reference, "material"),
                "structureCost": _add(prop(reference, "structureMaterial"), prop(reference, "structureLabour"), prop(reference, "structureRepair")),
                "installationCost": prop(reference, "installation"),
                "siteSurveyCost": None,
                "engineeringDesignCost": None,
                "transportationCost": prop(reference, "transportationBase"),
                "serviceAmcCost": prop(reference, "service"),
                "miscellaneousCost": prop(reference, "miscellaneous"),
                "officeExpenseAllocation": prop(reference, "office"),
                "specialProjectWorksCost": None,
                "directProjectCost": prop(internal, "referenceTotal"),
                "totalActualProjectCost": prop(internal, "referenceTotal"),
                "totalActualCost": prop(internal, "referenceTotal"),
                "referenceMarginPct": prop(internal, "marginPct"),
                "referenceGrand": prop(internal, "grand"),
                "marginVsMarket": prop(internal, "marginVsMarket"),
                "marginVsMarketPct": prop(internal, "marginVsMarketPct"),
                "landedCostCheck": landed_check,
                "traces": [trace],
            },
            "pricing": {
                "marginType": "PACK_MARKET_RATE",
                "targetGrossMargin": None,
                "sellingPriceBeforeGST": prop(customer, "sellingPriceBeforeGST"),
                "grossProfit": None,
                "gstRegime": prop(gst, "regime"),
                "gstRatePct": prop(gst, "ratePct"),
                "gstAmount": prop(customer, "gstAmount"),
                "sellingPriceIncludingGST": prop(customer, "sellingPriceIncludingGST"),
                "extras": extras,
                "extrasIncludingGST": transport_extra,
                "customerTotalIncludingGST": prop(customer, "customerTotalIncludingGST"),
                "customerSideExpenses": [],
                "marketRate": prop(customer, "marketRate"),
                "swapDeltaTotal": prop(customer, "swapDeltaTotal"),
                "roofAddOn": prop(customer, "roofAddOn"),
            },
            "pack": {
                "configVersion": config_version,
                "marketRateKey": prop(pack_pricing, "marketRateKey"),
                "marketRate": prop(customer, "marketRate"),
                "inputs": inputs,
                "swapDeltas": prop(pack_pricing, "swapDeltas"),
                "roofAddOnDetail": prop(pack_pricing, "roofAddOnDetail"),
                "structureMaterial": prop(pack_pricing, "structureMaterial"),
                "transport": detail,
                # the JavaScript default ([]) replaces only undefined: an explicit null stays null
                "materialList": None if material_list is None else [] if material_list is UNDEFINED else list(material_list),
            },
            "offer": _offer_section(offer),
        }
    )


def _positive(value: Any) -> bool:
    """JavaScript ``value > 0``."""
    number = js_number(value)
    return not number.is_nan() and number > 0


def apply_config_change(snapshot: Any, change: Any = None) -> Mapping:
    """``applyConfigChange``: a configuration change never mutates an issued snapshot (the same object comes back)."""
    return FrozenDict(
        snapshot=snapshot,
        mutated=False,
        effect="A configuration change produces a NEW calculation. Issued snapshots and the quotations built on them never change silently.",
        change=deep_freeze(_nullable(change)),
    )


def compare_recalculation(previous_snapshot: Any, cost: Any, pricing: Any) -> Mapping | None:
    """``recalculateForNewQuotation``'s comparison of a new cost/pricing run against a previous snapshot (the old
    snapshot is not touched). ``None`` without a previous snapshot."""
    if not js_truthy(previous_snapshot):
        return None

    def delta(before: Any, after: Any) -> Any:
        if is_nullish(before) or is_nullish(after):
            return None
        with localcontext(EXACT):
            result = js_number(after) - js_number(before)
        return result if result.is_finite() else None

    previous_cost, previous_pricing, previous_versions = prop(previous_snapshot, "cost"), prop(previous_snapshot, "pricing"), prop(previous_snapshot, "versions")
    changed = []
    for key in VERSION_KEYS:
        before = prop(previous_versions, key)
        if key == "pricingEngineVersion":
            after = prop(pricing, "pricingEngineVersion")
        elif key == "moneyRuleVersion":
            after = prop(previous_versions, "moneyRuleVersion")
        else:
            after = coalesce(prop(cost, key), prop(pricing, key), None)
        if not _strict_equal(before, after):
            changed.append(key)
    return deep_freeze(
        {
            "totalActualCost": {
                "before": prop(previous_cost, "totalActualCost"),
                "after": prop(cost, "totalActualCost"),
                "delta": delta(prop(previous_cost, "totalActualCost"), prop(cost, "totalActualCost")),
            },
            "sellingPriceBeforeGST": {
                "before": prop(previous_pricing, "sellingPriceBeforeGST"),
                "after": prop(pricing, "sellingPriceBeforeGST"),
                "delta": delta(prop(previous_pricing, "sellingPriceBeforeGST"), prop(pricing, "sellingPriceBeforeGST")),
            },
            "versionsChanged": changed,
        }
    )


def supersede(snapshot: Mapping, *, superseded_by: Any = None, at: Any = None) -> Mapping:
    """``supersede``: a SUPERSEDED copy; the original stands."""
    return deep_freeze({**snapshot, "status": CommercialSnapshotStatus.SUPERSEDED.value, "supersededBy": superseded_by, "supersededAt": at})


# ---------------------------------------------------------------------------------------------------------------------
# commercialFreeze.js — document.snapshot (schema flarize.commercial-snapshot/1)
# ---------------------------------------------------------------------------------------------------------------------


class FreezeError(ValueError):
    """A refused freeze: ``ISSUED_AT_REQUIRED``, ``DEMO_BRANDING_BLOCKED_IN_PRODUCTION``,
    ``DEMO_TRANSPORT_RATE_BLOCKED_IN_PRODUCTION`` (``detail`` names the demo kinds / the rate source)."""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def build_commercial_snapshot(
    *,
    issued_at: Any,
    bom: Any = None,
    project: Any = None,
    transportation_config: Any = None,
    commercial_snapshot: Any = None,
    subsidy_result: Any = None,
    branding_store: Any = None,
    branding_overrides: Mapping | None = None,
    policy: Any = None,
    validity_override_days: Any = None,
    renderer_pin: Any = None,
    package_profile: Any = None,
    production_mode: bool = False,
) -> Mapping:
    """``buildCommercialSnapshot``: every commercial input a historical quotation must reproduce, deep-cloned and frozen
    (engineering, battery, package, procurement, transportation, commercial, subsidy, branding, validity, pack, pin).

    In ``production_mode`` a demo branding account or a demo transport rate refuses the freeze, and an unconfigured
    validity policy is fatal (outside production the validity stays ``None``).
    """
    if not js_truthy(issued_at):
        raise FreezeError("ISSUED_AT_REQUIRED", "commercialFreeze.buildCommercialSnapshot: issuedAt required")
    project = project if js_truthy(project) else {}
    bom_engineering = prop(bom, "engineering")
    engineering = (
        {"status": prop(bom_engineering, "status") if js_truthy(prop(bom_engineering, "status")) else None, "blockers": prop(bom_engineering, "blockers") or []} if js_truthy(bom_engineering) else None
    )
    config = prop(bom, "systemConfig")
    battery = (
        {
            "batteryQuantity": _nullable(prop(config, "batteryQuantity")),
            "batterySource": _nullable(prop(config, "batterySource")),
            "batteryIncluded": _nullable(prop(config, "batteryIncluded")),
        }
        if js_truthy(config)
        else None
    )
    package = (
        {
            "systemType": _nullable(prop(config, "systemType")),
            "size": _nullable(prop(config, "size")),
            "phase": _nullable(prop(config, "phase")),
            "tier": _nullable(prop(config, "tier")),
            "profileKey": _nullable(prop(config, "profileKey")),
            "isPremiumTier": prop(config, "tier") == "premium",
            "profileNotes": _nullable(prop(package_profile, "notes")),
        }
        if js_truthy(config)
        else None
    )

    snapshot = commercial_snapshot if js_truthy(commercial_snapshot) else None
    pinned = coalesce(prop(snapshot, "versions"), snapshot, None) if snapshot is not None else None
    procurement = (
        {
            "procurementPriceVersion": _nullable(prop(pinned, "procurementPriceVersion")),
            "landedCostVersion": _nullable(prop(pinned, "landedCostVersion")),
            "catalogVersion": _nullable(prop(pinned, "catalogVersion")),
        }
        if js_truthy(pinned)
        else None
    )

    cost_section = coalesce(prop(snapshot, "cost"), snapshot, None) if snapshot is not None else None
    traces = prop(cost_section, "traces")
    trace = next((item for item in traces if js_truthy(item) and prop(item, "head") == "TRANSPORTATION"), None) if isinstance(traces, (list, tuple)) else None
    vehicle = next((item for item in (js_array(prop(transportation_config, "vehicles")) or []) if _strict_equal(prop(item, "vehicleType"), prop(project, "vehicleType"))), None)
    trace_inputs = prop(trace, "inputs")
    transportation = None
    if js_truthy(prop(project, "vehicleType")) or not is_nullish(prop(project, "distanceKm")) or trace is not None:
        basis = prop(trace_inputs, "distanceBasis")
        basis = basis if js_truthy(basis) else prop(transportation_config, "distanceBasis")
        transportation = {
            "vehicleType": coalesce(prop(trace_inputs, "vehicleType"), prop(project, "vehicleType"), None),
            "distanceKm": coalesce(prop(trace_inputs, "distanceKm"), prop(project, "distanceKm"), None),
            "distanceBasis": basis if js_truthy(basis) else "ONE_WAY",
            "ratePerKm": coalesce(prop(trace_inputs, "ratePerKm"), prop(vehicle, "ratePerKm"), None),
            "rateVersion": coalesce(prop(trace, "version"), prop(vehicle, "version"), prop(transportation_config, "version"), None),
            "rateSource": coalesce(prop(trace, "source"), "COST_CONFIG_VEHICLES" if vehicle is not None else None),
            "amount": _nullable(prop(trace, "amount")),
            "formula": "distanceKm × vehicle.ratePerKm",
        }

    commercial = None
    if snapshot is not None:
        cost_values = coalesce(prop(snapshot, "cost"), snapshot)
        pricing_values = coalesce(prop(snapshot, "pricing"), snapshot)
        target_margin = prop(pricing_values, "targetGrossMargin")
        commercial = {
            "totalActualProjectCost": coalesce(prop(cost_values, "totalActualProjectCost"), prop(snapshot, "totalActualProjectCost"), None),
            "marginType": coalesce(prop(pricing_values, "marginType"), prop(snapshot, "marginType"), None),
            "targetMarginPct": _times_100(target_margin) if not is_nullish(target_margin) else _nullable(prop(snapshot, "targetMarginPct")),
            "minimumMarginPct": _nullable(prop(snapshot, "minimumMarginPct")),
            "listSellingPriceBeforeGST": _nullable(prop(snapshot, "listSellingPriceBeforeGST")),
            "discount": _nullable(prop(snapshot, "discount")),
            "discountAmount": _nullable(prop(snapshot, "discountAmount")),
            "sellingPriceBeforeGST": coalesce(prop(pricing_values, "sellingPriceBeforeGST"), prop(snapshot, "sellingPriceBeforeGST"), None),
            "finalSellingPriceBeforeGST": _nullable(prop(snapshot, "finalSellingPriceBeforeGST")),
            "gstRegime": _nullable(prop(snapshot, "gstRegime")),
            "gstRatePct": coalesce(prop(pricing_values, "gstRatePct"), prop(snapshot, "gstRatePct"), None),
            "gstAmount": coalesce(prop(pricing_values, "gstAmount"), prop(snapshot, "gstAmount"), None),
            "sellingPriceIncludingGST": coalesce(prop(pricing_values, "sellingPriceIncludingGST"), prop(snapshot, "sellingPriceIncludingGST"), None),
            "marginVersion": coalesce(prop(pinned, "marginVersion"), prop(snapshot, "marginVersion"), None),
            "gstVersion": coalesce(prop(pinned, "gstVersion"), prop(snapshot, "gstVersion"), None),
            "pricingMode": coalesce(prop(snapshot, "pricingMode"), "GROSS_MARGIN"),
            "marketRate": _nullable(prop(pricing_values, "marketRate")),
            "swapDeltaTotal": _nullable(prop(pricing_values, "swapDeltaTotal")),
            "roofAddOn": _nullable(prop(pricing_values, "roofAddOn")),
            "extrasIncludingGST": _nullable(prop(pricing_values, "extrasIncludingGST")),
            "customerTotalIncludingGST": _nullable(prop(pricing_values, "customerTotalIncludingGST")),
        }

    subsidy = (
        {
            "available": _nullable(prop(subsidy_result, "available")),
            "subsidyType": _nullable(prop(subsidy_result, "subsidyType")),
            "centralSubsidy": _nullable(prop(subsidy_result, "centralSubsidy")),
            "stateSubsidy": _nullable(prop(subsidy_result, "stateSubsidy")),
            "totalSubsidy": _nullable(prop(subsidy_result, "totalSubsidy")),
            "eligibilityStatus": _nullable(prop(subsidy_result, "eligibilityStatus")),
            "schemeName": _nullable(prop(subsidy_result, "schemeName")),
            "calculatedAt": _nullable(prop(subsidy_result, "calculatedAt")),
            "source": "SUBSIDY_ENGINE (separate from cost/margin per locked 2.7)",
        }
        if js_truthy(subsidy_result)
        else None
    )

    branding = freeze_branding_snapshot(branding_store, branding_overrides or {}) if js_truthy(branding_store) else None
    if production_mode and branding:
        demo = demo_branding_kinds(branding)
        if demo:
            raise FreezeError(
                "DEMO_BRANDING_BLOCKED_IN_PRODUCTION",
                f"DEMO_BRANDING_BLOCKED_IN_PRODUCTION: cannot issue a production quotation while branding {', '.join(demo)} is flagged isDemo:true. "
                "Publish a non-demo account or lift productionMode.",
                {"demoKinds": list(demo)},
            )
    if production_mode and transportation and transportation["ratePerKm"] is not None:
        from_rate_card = transportation["rateSource"] == "PROJECT_HEAD_RATE_CARD"
        demo_config = prop(transportation_config, "isDemo") is True or (vehicle is not None and prop(vehicle, "isDemo") is True)
        if not from_rate_card and demo_config:
            raise FreezeError(
                "DEMO_TRANSPORT_RATE_BLOCKED_IN_PRODUCTION",
                f'DEMO_TRANSPORT_RATE_BLOCKED_IN_PRODUCTION: vehicle rate for "{js_string(transportation["vehicleType"])}" resolved from cost-config UAT reference data '
                "(isDemo:true). Project Head must publish the vehicle rate through the versioned rate card (PUT /api/workspace/commercial/rate-card) before production issuance.",
                {"vehicleType": transportation["vehicleType"], "rateSource": transportation["rateSource"], "rateVersion": transportation["rateVersion"]},
            )

    validity = None
    if js_truthy(policy):
        try:
            validity = resolve_effective_validity(policy, validity_override_days, issued_at)
        except PolicyError as error:
            if production_mode or error.code != PolicyErrorCode.POLICY_NOT_CONFIGURED:
                raise
            validity = None

    pack = prop(snapshot, "pack") if snapshot is not None else None
    return deep_freeze(
        {
            "schemaVersion": "flarize.commercial-snapshot/1",
            "frozenAt": issued_at,
            "engineering": engineering,
            "battery": battery,
            "package": package,
            "procurement": procurement,
            "transportation": transportation,
            "commercial": commercial,
            "subsidy": subsidy,
            "branding": branding,
            "validity": validity,
            "pack": pack if js_truthy(pack) else None,
            "rendererPin": _spread(renderer_pin) if js_truthy(renderer_pin) else None,
        }
    )


def _times_100(fraction: Any) -> Decimal | None:
    """A fraction as a percentage (``0.2`` → ``20``), exactly."""
    number = js_number(fraction)
    if not number.is_finite():
        return None
    with localcontext(EXACT):
        return number * 100


# ---------------------------------------------------------------------------------------------------------------------
# quotationPolicy.js — effective validity (append-only policies, one resolution order)
# ---------------------------------------------------------------------------------------------------------------------


class PolicyErrorCode(StrEnum):
    POLICY_NOT_CONFIGURED = "POLICY_NOT_CONFIGURED"
    VALIDITY_INVALID = "VALIDITY_INVALID"
    POLICY_EDITOR_FORBIDDEN = "POLICY_EDITOR_FORBIDDEN"
    POLICY_WINDOW_INVALID = "POLICY_WINDOW_INVALID"
    POLICY_NOT_ACTIVE = "POLICY_NOT_ACTIVE"


class PolicyError(ValueError):
    def __init__(self, code: PolicyErrorCode, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


_DAY_MS = 86_400_000
_POLICY_SCHEMA_V2 = "flarize.quotation-policy/2"


def _positive_int(value: Any) -> bool:
    """``typeof n === 'number' && Number.isInteger(n) && n > 0``."""
    return is_number(value) and Decimal(value).is_finite() and Decimal(value) == Decimal(value).to_integral_value() and value > 0


def _iso_ms(value: Any, field: str) -> int:
    millis = parse_iso_ms(value)
    if millis is None:
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, f'Invalid {field} "{js_string(value)}".')
    return millis


def _add_days(iso: Any, days: Any) -> str:
    return iso_from_ms(_iso_ms(iso, "issuedAt") + int(days) * _DAY_MS)


def validate_policy_record(policy: Any) -> None:
    """Refuse a malformed effective-period policy (write time and resolve time)."""
    if not js_truthy(policy) or not _is_js_object(policy):
        raise PolicyError(PolicyErrorCode.POLICY_WINDOW_INVALID, "Policy record is not an object.")
    policy_id = prop(policy, "policyId")
    if not js_truthy(policy_id) or not isinstance(policy_id, str):
        raise PolicyError(PolicyErrorCode.POLICY_WINDOW_INVALID, "Policy.policyId required.")
    if not _positive_int(prop(policy, "validityDays")):
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, f'Policy {policy_id} validityDays "{js_string(prop(policy, "validityDays"))}" is not a positive integer.')
    if not js_truthy(prop(policy, "effectiveFrom")):
        raise PolicyError(PolicyErrorCode.POLICY_WINDOW_INVALID, f"Policy {policy_id} effectiveFrom required.")
    start = _iso_ms(policy["effectiveFrom"], f"policy {policy_id} effectiveFrom")
    end_raw = prop(policy, "effectiveTo")
    end = None if is_nullish(end_raw) else _iso_ms(end_raw, f"policy {policy_id} effectiveTo")
    if end is not None and end <= start:
        raise PolicyError(
            PolicyErrorCode.POLICY_WINDOW_INVALID,
            f"Policy {policy_id} effectiveTo ({js_string(end_raw)}) must be after effectiveFrom ({js_string(policy['effectiveFrom'])}).",
        )
    status = prop(policy, "status")
    if js_truthy(status) and status not in ("ACTIVE", "INACTIVE"):
        raise PolicyError(PolicyErrorCode.POLICY_NOT_ACTIVE, f'Policy {policy_id} status "{js_string(status)}" not recognised (ACTIVE|INACTIVE).')


def resolve_active_policy(store: Any, issued_at: Any) -> Mapping | None:
    """The ACTIVE policy whose window ``[effectiveFrom, effectiveTo)`` contains ``issued_at``; the latest
    ``effectiveFrom`` wins, then ``policyId`` descending. Malformed records are skipped, not fatal."""
    policies = js_array(prop(store, "policies")) or []
    if not policies:
        return None
    moment = _iso_ms(issued_at, "issuedAt")
    matches = []
    for policy in policies:
        if not js_truthy(policy) or prop(policy, "status") != "ACTIVE":
            continue
        try:
            validate_policy_record(policy)
        except PolicyError:
            continue
        start = _iso_ms(policy["effectiveFrom"], "effectiveFrom")
        end = None if is_nullish(prop(policy, "effectiveTo")) else _iso_ms(policy["effectiveTo"], "effectiveTo")
        if start <= moment and (end is None or moment < end):
            matches.append((start, policy))
    if not matches:
        return None
    matches.sort(key=lambda item: item[1]["policyId"], reverse=True)
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def resolve_effective_validity(store: Any, override_days: Any, issued_at: Any) -> Mapping:
    """``resolveEffectiveValidity``: per-quote override (``PROJECT_HEAD_OVERRIDE``) → ACTIVE effective policy
    (``EFFECTIVE_POLICY``) → admin default (``ADMIN_DEFAULT``) → ``POLICY_NOT_CONFIGURED``.
    ``validUntil = issuedAt + days × 86 400 000 ms``."""
    has_override = _positive_int(override_days)
    if not is_nullish(override_days) and not has_override:
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, f'Validity override "{js_string(override_days)}" is not a positive integer.')
    default = prop(prop(store, "validity"), "version")
    if has_override:
        return deep_freeze({"validityDays": override_days, "validUntil": _add_days(issued_at, override_days), "source": "PROJECT_HEAD_OVERRIDE", "policyVersion": _nullable(default)})
    active = resolve_active_policy(store, issued_at)
    if active is not None:
        return deep_freeze(
            {
                "validityDays": active["validityDays"],
                "validUntil": _add_days(issued_at, active["validityDays"]),
                "source": "EFFECTIVE_POLICY",
                "policyId": active["policyId"],
                "policyVersion": _nullable(prop(active, "version")),
                "effectiveFrom": active["effectiveFrom"],
                "effectiveTo": _nullable(prop(active, "effectiveTo")),
            }
        )
    days = prop(prop(store, "validity"), "defaultDays")
    if _positive_int(days):
        return deep_freeze({"validityDays": days, "validUntil": _add_days(issued_at, days), "source": "ADMIN_DEFAULT", "policyVersion": _nullable(default)})
    raise PolicyError(
        PolicyErrorCode.POLICY_NOT_CONFIGURED,
        "No ACTIVE effective-period policy contains this issuedAt, no Admin default is set, and no PH override was supplied. Cannot resolve validity.",
    )


def _clone_policy_store(store: Any) -> dict:
    clone = thaw(store) if isinstance(store, Mapping) else {}
    if not js_truthy(clone.get("schema")):
        clone["schema"] = _POLICY_SCHEMA_V2
    if not isinstance(clone.get("validity"), Mapping):
        clone["validity"] = {"defaultDays": None, "version": None, "updatedBy": None, "updatedAt": None, "status": "NOT_CONFIGURED"}
    if not isinstance(clone.get("policies"), list):
        clone["policies"] = []
    return clone


def publish_policy(store: Any, *, actor_id: str, policy: Mapping, at: str) -> Mapping:
    """Append a new effective-period policy (append-only: a duplicate ``policyId`` is refused)."""
    if not at:
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, "Timestamp (at) is required.")
    clone = _clone_policy_store(store)
    record = {
        "policyId": prop(policy, "policyId"),
        "validityDays": prop(policy, "validityDays"),
        "effectiveFrom": prop(policy, "effectiveFrom"),
        "effectiveTo": _nullable(prop(policy, "effectiveTo")),
        "status": prop(policy, "status") if js_truthy(prop(policy, "status")) else "ACTIVE",
        "version": prop(policy, "version") if js_truthy(prop(policy, "version")) else f"{js_string(prop(policy, 'policyId'))}@{at}",
        "createdBy": actor_id,
        "createdAt": at,
        "note": _nullable(prop(policy, "note")),
    }
    validate_policy_record(record)
    if any(prop(existing, "policyId") == record["policyId"] for existing in clone["policies"]):
        raise PolicyError(
            PolicyErrorCode.POLICY_WINDOW_INVALID,
            f'Policy "{record["policyId"]}" already exists. Policies are immutable — publish a new policyId.',
            {"policyId": record["policyId"]},
        )
    clone["policies"].append(thaw(record))
    return deep_freeze(clone)


def set_policy_status(store: Any, *, actor_id: str, policy_id: str, status: str, at: str, reason: str | None = None) -> Mapping:
    """ACTIVE ⇄ INACTIVE; a policy is never deleted."""
    if not at:
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, "Timestamp (at) is required.")
    if status not in ("ACTIVE", "INACTIVE"):
        raise PolicyError(PolicyErrorCode.POLICY_NOT_ACTIVE, f'status must be ACTIVE or INACTIVE (got "{js_string(status)}").')
    clone = _clone_policy_store(store)
    policy = next((item for item in clone["policies"] if prop(item, "policyId") == policy_id), None)
    if policy is None:
        raise PolicyError(PolicyErrorCode.POLICY_WINDOW_INVALID, f'Policy "{js_string(policy_id)}" not found.', {"policyId": policy_id})
    policy.update(status=status, statusChangedBy=actor_id, statusChangedAt=at)
    if reason:
        policy["statusChangeReason"] = reason
    return deep_freeze(clone)


def set_default_validity_days(store: Any, *, actor_id: str, default_days: Any, at: str) -> Mapping:
    """The v1 fallback default (fills gaps between policy windows); ``None`` unconfigures it."""
    if not at:
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, "Timestamp (at) is required.")
    if default_days is not None and not _positive_int(default_days):
        raise PolicyError(PolicyErrorCode.VALIDITY_INVALID, f'defaultDays "{js_string(default_days)}" is not a positive integer (or null).')
    clone = _clone_policy_store(store)
    clone["validity"] = {
        **clone["validity"],
        "defaultDays": default_days,
        "version": f"DEFAULT@{at}",
        "updatedBy": actor_id,
        "updatedAt": at,
        "status": "NOT_CONFIGURED" if default_days is None else "CONFIGURED",
    }
    return deep_freeze(clone)


def describe_policy_store(store: Any, now: Any = None) -> Mapping:
    """Read-side summary: policies sorted by ``effectiveFrom``, the policy active at ``now``, whether anything is configured."""
    clone = _clone_policy_store(store)
    active = None
    if js_truthy(now):
        try:
            active = resolve_active_policy(clone, now)
        except PolicyError:
            active = None
    policies = sorted(clone["policies"], key=lambda item: js_string(prop(item, "effectiveFrom")))
    return deep_freeze(
        {
            "schema": clone["schema"],
            "validity": clone["validity"],
            "policies": policies,
            "activeNow": {"policyId": active["policyId"], "validityDays": active["validityDays"]} if active is not None else None,
            "configured": any(prop(item, "status") == "ACTIVE" for item in clone["policies"]) or _positive_int(prop(clone["validity"], "defaultDays")),
        }
    )
