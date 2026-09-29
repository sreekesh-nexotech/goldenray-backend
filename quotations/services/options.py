"""``GET quotations/system-options/`` — what a quotation can offer now (Flarize ``/api/sales/system-options`` on the
current PackRelease): per system type the sizes (with phase and future-ready targets), tiers, battery configurations
and the released packs with their customer price; the roof types, vehicles and transport rule of the release's
configuration; subsidy types; the tier display names. Only released packs are offered — a size without any released
pack is not listed."""

from __future__ import annotations

from packs.models import ReleasePack
from quotations.models import RoofType, SubsidyType, SystemType, TierDisplayName
from quotations.services.common import ENGINE_SYSTEM
from quotations.services.inputs import current_pack_release

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
