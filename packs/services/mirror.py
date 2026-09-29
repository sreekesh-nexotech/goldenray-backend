"""The typed mirror of a config version (``packs_config_pack`` + ``packs_config_line``), derived from its JSON.

:func:`mirror` enumerates the packs of ``version.config``, builds each one's BOM with the version's pins and the current
PriceRelease's catalog, and writes the result: packs are upserted by key (so their pins survive), packs the
configuration no longer offers are soft-deleted, and the lines — derived rows — are replaced. A pack the BOM builder
refuses keeps its row with ``build_error`` and no lines. Lines: ``SLOT`` (resolved by the template), ``MANUAL`` (resolved
from the pack's pin), ``FIXED`` (fixed items), ``STRUCTURE`` (the flat-roof structure material the base price assumes).

Called inside the caller's transaction by every write that changes the configuration or the pins.
"""

from __future__ import annotations

from decimal import Decimal

from bom.models import StructureTemplate
from catalog.models import Component
from core.services import stamp_create
from packs.models import ConfigLine, ConfigPack, ConfigVersion, LineSource
from packs.services import engine
from packs.services.common import platform_system, platform_tier, size_kw
from packs.services.context import EngineContext, engine_context, version_pins

PACK_FIELDS = (
    "system_type",
    "tier",
    "size_key",
    "size_kw",
    "phase",
    "battery_config",
    "future_size_key",
    "future_size_kw",
    "is_future_ready",
    "panel",
    "inverter",
    "battery",
    "battery_qty",
    "structure_template",
    "profile_key",
    "market_rate_key",
    "display_name",
    "build_error",
    "sort_order",
)


def _qty(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001 - a non-numeric engine quantity is stored as 0 (the engine skips such lines)
        return Decimal("0")


def line_source(line: dict) -> str:
    if line.get("category") == "fixed":
        return LineSource.FIXED
    if str(line.get("selectionMethod") or "").startswith("PACKAGE_REGISTRY"):
        return LineSource.MANUAL
    return LineSource.SLOT


def mirror(version: ConfigVersion, *, user, ctx: EngineContext | None = None) -> dict:
    if version.config is None:
        return {"packs": 0, "built": 0, "refused": 0}
    ctx = ctx or engine_context()
    config = version.config
    pins = version_pins(version, removed_packs=True)
    specs = engine.enumerate_packs(config)
    # Soft-deleted packs too: a pack the configuration offers again is restored with its pins (a live row wins).
    existing: dict[str, ConfigPack] = {}
    for pack in ConfigPack.all_objects.filter(config_version=version).order_by("-id"):
        if pack.key not in existing or (existing[pack.key].deleted_at is not None and pack.deleted_at is None):
            existing[pack.key] = pack
    flat_roof = StructureTemplate.objects.filter(slug="flat_roof").first()
    built: dict[str, tuple[engine.PackSpec, dict | None]] = {}
    skus: set[str] = set()
    for spec in specs:
        try:
            bom = engine.build(spec, config=config, catalog=ctx.catalog, pins=pins.get(spec.key, []))
        except engine.EngineError as exc:
            built[spec.key] = (spec, {"error": str(getattr(exc, "message", exc))[:255]})
            continue
        built[spec.key] = (spec, bom)
        skus.update(line["componentId"] for line in bom["lines"] if line.get("componentId"))
    components = {component.sku: component for component in Component.objects.filter(sku__in=skus)}
    packs: dict[str, ConfigPack] = {}
    for key, (spec, bom) in built.items():
        values = _pack_values(spec, bom, config, components, flat_roof)
        pack = existing.get(key)
        if pack is None:
            pack = ConfigPack(config_version=version, key=key, **values)
            stamp_create(pack, user)
            pack.save()
        else:
            if pack.deleted_at is not None:
                pack.restore(user)
            changed = {name: value for name, value in values.items() if getattr(pack, name) != value}
            if changed:
                pack.versioned_update(user, **changed)
        packs[key] = pack
    for key, pack in existing.items():
        if key not in built and pack.deleted_at is None:
            pack.soft_delete(user)
    for spec in specs:
        if spec.future:
            pack, standard = packs[spec.key], packs.get(engine.PackSpec(spec.system, spec.size, spec.tier, spec.phase, "", spec.battery).key)
            if standard is not None and pack.pair_of_id != standard.pk:
                pack.versioned_update(user, pair_of=standard)
    pack_ids = {pack.pk for pack in existing.values()} | {pack.pk for pack in packs.values()}
    ConfigLine.all_objects.filter(pack_id__in=pack_ids).delete()  # derived rows, rebuilt below (no join: a plain DELETE … IN)
    lines = []
    for key, (spec, bom) in built.items():
        if bom is None or "error" in bom:
            continue
        pack = packs[key]
        for index, line in enumerate(bom["lines"]):
            component = components.get(line.get("componentId")) if line.get("componentId") else None
            source = line_source(line)
            if component is None and source != LineSource.FIXED:
                source = LineSource.FIXED
            row = ConfigLine(
                pack=pack,
                slot_key=str(line.get("category") or "")[:32],
                component=component,
                name=str(line.get("name") or "")[:255],
                qty=_qty(line.get("qty")),
                source=source,
                selection_method=str(line.get("selectionMethod") or "")[:40],
                sort_order=index,
            )
            stamp_create(row, user)
            lines.append(row)
        for index, item in enumerate(engine.structure_lines(spec, config)):
            row = ConfigLine(pack=pack, slot_key="structure", component=None, name=str(item.get("name") or "")[:255], qty=_qty(item.get("qty")), source=LineSource.STRUCTURE, sort_order=1000 + index)
            stamp_create(row, user)
            lines.append(row)
    ConfigLine.objects.bulk_create(lines, batch_size=1000)
    refused = sum(1 for _, bom in built.values() if bom is None or "error" in bom)
    return {"packs": len(built), "built": len(built) - refused, "refused": refused, "lines": len(lines)}


def _first(bom: dict, category: str, components: dict) -> Component | None:
    for line in bom.get("lines") or []:
        if line.get("category") == category and line.get("componentId"):
            return components.get(line["componentId"])
    return None


def _pack_values(spec: engine.PackSpec, bom: dict | None, config: dict, components: dict, flat_roof) -> dict:
    ok = bom is not None and "error" not in bom
    system_config = (bom or {}).get("systemConfig") or {}
    battery_qty = system_config.get("batteryQuantity") if ok else (spec.battery or 0)
    return {
        "system_type": platform_system(spec.system),
        "tier": platform_tier(spec.tier),
        "size_key": spec.size,
        "size_kw": size_kw(spec.size),
        "phase": spec.phase,
        "battery_config": spec.battery_config,
        "future_size_key": spec.future,
        "future_size_kw": size_kw(spec.future) if spec.future else None,
        "is_future_ready": bool(spec.future),
        "panel": _first(bom, "panel", components) if ok else None,
        "inverter": _first(bom, "inverter", components) if ok else None,
        "battery": _first(bom, "battery", components) if ok else None,
        "battery_qty": int(battery_qty or 0) if str(battery_qty or 0).isdigit() else 0,
        "structure_template": flat_roof,
        "profile_key": str(system_config.get("profileKey") or "")[:32],
        "market_rate_key": spec.market_rate_key[:48],
        "display_name": engine.display_name(spec, config),
        "build_error": "" if ok else (bom or {}).get("error", "refused")[:255],
        "sort_order": spec.sort_order,
    }
