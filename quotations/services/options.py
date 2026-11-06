"""``GET quotations/system-options/`` — what a quotation can offer now (Flarize ``/api/sales/system-options`` on the
current PackRelease): per system type the sizes (with phase and future-ready targets), tiers, battery configurations
and the released packs with their customer price; the roof types, vehicles and transport rule of the release's
configuration; subsidy types; the tier display names. Only released packs are offered — a size without any released
pack is not listed."""

from __future__ import annotations

from decimal import Decimal

from core.errors import Conflict, DomainError
from packs.models import ReleasePack
from quotations.models import RoofType, SubsidyType, SystemType, TierDisplayName
from quotations.services import pipeline
from quotations.services.common import ENGINE_SYSTEM, ENGINE_TIER, phase_for
from quotations.services.inputs import _template, current_pack_release

SYSTEM_LABELS = {SystemType.ONGRID: "On-grid", SystemType.HYBRID: "Hybrid"}


def system_options(*, system_type: str | None = None, phase: str | None = None) -> dict:
    release = current_pack_release()
    config = release.config_version.config or {}
    packs = ReleasePack.objects.filter(release=release).order_by("sort_order")
    if system_type:
        packs = packs.filter(system_type=system_type)
    if phase:
        packs = packs.filter(phase=phase)
    options: dict = {}
    for pack in packs:
        template = (config.get("bomTemplates") or {}).get(ENGINE_SYSTEM[pack.system_type]) or {}
        entry = options.setdefault(pack.system_type, {"label": str(SYSTEM_LABELS[pack.system_type]), "sizes": {}, "tiers": [], "battery_configs": [], "packs": []})
        size = entry["sizes"].setdefault(pack.size_key, {"size_key": pack.size_key, "label": (template.get("sizes") or {}).get(pack.size_key, pack.size_key), "phase": pack.phase, "future_ready": []})
        if pack.future_size_key and pack.future_size_key not in size["future_ready"]:
            size["future_ready"].append(pack.future_size_key)
        if pack.tier not in entry["tiers"]:
            entry["tiers"].append(pack.tier)
        if pack.battery_config and pack.battery_config not in entry["battery_configs"]:
            entry["battery_configs"].append(pack.battery_config)
        entry["packs"].append(
            {
                "key": pack.key,
                "tier": pack.tier,
                "size_key": pack.size_key,
                "phase": pack.phase,
                "battery_config": pack.battery_config,
                "future_size_key": pack.future_size_key,
                "display_name": pack.display_name,
                "customer_price_incl_gst": str(pack.customer_price_incl_gst),
            }
        )
    for entry in options.values():
        entry["sizes"] = list(entry["sizes"].values())
    matrix = config.get("installationMatrix") or {}
    roofs = sorted({kind.upper() for row in matrix.values() if isinstance(row, dict) for kind in row} & set(RoofType.values)) or list(RoofType.values)
    transport = config.get("transportConfig") or {}
    names = {}
    for row in TierDisplayName.objects.order_by("system_type", "tier"):
        names.setdefault(row.system_type, {})[row.tier] = {"en": row.name_en, "ml": row.name_ml, "recommended": row.is_recommended, "badge_en": row.badge_en, "badge_ml": row.badge_ml}
    return {
        "pack_release": release.number,
        "options": options,
        "roof_types": roofs,
        "vehicles": transport.get("vehicles") or [],
        "transport": {"included_km": transport.get("baseDistanceKm"), "distance_basis": "ONE_WAY"},
        "subsidy_types": list(SubsidyType.values),
        "tier_names": names,
    }


def component_options(*, system_type: str, tier: str, size_key: str, phase: str = "", battery_config: str = "", future_size_key: str = "") -> dict:
    """``GET quotations/component-options/`` — for one configuration of the current PackRelease, the parts Sales may
    choose per swappable slot (the panel above all) with the pack's default marked.

    The same list :func:`quotations.services.pipeline.validate_selections` checks a quotation's
    ``selections.tier_selections`` against, so a choice offered here is one a quotation accepts. Prices are not
    included (Sales rules). 409 ``no_approved_package`` when the release has no such pack.
    """
    release = current_pack_release()
    template = _template(release, system_type)
    if size_key not in (template.get("sizes") or {}):
        raise DomainError("validation_error", "Unknown size.", errors={"size_key": [f"Not a size of the {system_type} template."]})
    phase = phase or phase_for(size_key, template.get("threePhase") or [])
    battery = battery_config if system_type == SystemType.HYBRID else ""
    exists = ReleasePack.objects.filter(release=release, system_type=system_type, tier=tier, size_key=size_key, phase=phase, battery_config=battery, future_size_key=future_size_key).exists()
    if not exists:
        raise Conflict("no_approved_package", f"PackRelease #{release.number} has no {system_type} {size_key} {tier} {phase} pack{f' with {battery} batteries' if battery else ''}.")
    request = pipeline.Request(
        system=ENGINE_SYSTEM[system_type],
        size=size_key,
        tier=ENGINE_TIER[tier],
        phase=phase,
        battery=int(battery) if battery else None,
        future=future_size_key or None,
        roof=RoofType.FLAT,
        distance_km=Decimal("0"),
        vehicle=None,
        tier_selections={},
        offer_code="",
        subsidy_type=SubsidyType.NONE,
        ghs_houses=None,
        language="en",
        appliance_rows=None,
        validity_override_days=None,
    )
    try:
        alternatives = pipeline.approved_alternatives(pipeline.ReleaseContext(release), request, ENGINE_TIER[tier])
    except pipeline.TierFailure as failure:
        raise Conflict(failure.code.lower(), failure.message) from None
    slots = []
    for category, items in alternatives.items():
        choices = [
            {
                "id": item.get("id"),
                "name": item.get("name") or item.get("id"),
                "brand": item.get("brand"),
                "watt": item.get("watt"),
                "dcr": item.get("dcr"),
                "is_default": bool(item.get("isDefault")),
            }
            for item in items or []
            if isinstance(item, dict) and item.get("id")
        ]
        if choices:
            slots.append({"category": category, "default_id": next((c["id"] for c in choices if c["is_default"]), choices[0]["id"]), "options": choices})
    return {"pack_release": release.number, "system_type": system_type, "tier": tier, "size_key": size_key, "phase": phase, "battery_config": battery, "slots": slots}
