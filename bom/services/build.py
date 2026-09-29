"""``POST bom/build/`` — a dry BOM for (system type, size, tier, phase, battery band, structure) through
``engines.bom_builder.build_bom`` (the Flarize pack BOM builder), nothing persisted.

The engine reads a Flarize-shaped catalog; :func:`flarize_catalog` builds it from the platform tables: the template's
slots (``qty_rule`` → ``qty`` / ``premiumQty`` / ``batQty`` / ``premiumBatQty``, other rule types evaluated per size
key), its fixed items whose ``condition`` holds (price = the item's ``unit_price``, else the component's current LIST
price), the slot categories' live, non-retired components with their tiers, legacy phase/type codes and current LIST
prices, and the package profiles. The structure template (when asked) adds its lines (quantities over the system kW)
and the tube weight. Required slots that need a component and got none are reported as warnings.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Q

from bom.models import FixedItem, PackageProfile, Slot, StructureItemType, StructureTemplate, Template
from bom.services.common import percent_of, validation_error
from bom.services.qty_rules import QtyContext, evaluate, matches, size_table_for_flarize
from catalog.models import Category, Component, ComponentStatus
from core.errors import DomainError
from engines.bom_builder import BomBuildError, build_bom
from pricing.services.common import parse_size_key

ENGINE_STATUS = {ComponentStatus.ACTIVE: "ACTIVE", ComponentStatus.DEPRECATED: "ACTIVE", ComponentStatus.DRAFT: "DRAFT"}
ACCESSORY_CATEGORIES = ("enphase", "ug_cable")  # read by the engine's premium accessory step


def _number(value: Decimal | None):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _kw(size_key: str) -> float:
    parsed = parse_size_key(size_key)
    return float(parsed[0]) if parsed else 0.0


def _table(rule: dict, sizes: list[str], three_phase: set[str]) -> dict:
    if rule.get("type") == "size_table":
        return size_table_for_flarize(rule)
    return {"qty": {size: evaluate(rule, QtyContext(size_key=size, kw=_kw(size), is_three_phase=size in three_phase)) for size in sizes}}


def _item(component: Component, price) -> dict:
    spec = getattr(component, "inverter_spec", None)
    panel = getattr(component, "panel_spec", None)
    attributes = component.attributes or {}
    if spec is not None:
        kind = "micro" if spec.topology == "MICRO" else {"ONGRID": "ongrid", "HYBRID": "hybrid"}.get(spec.inverter_type)
    else:
        kind = attributes.get("type")
    item = {
        "id": component.sku,
        "name": component.name,
        "brand": component.brand_label or None,
        "price": _number(price),
        "tiers": [tier.tier.lower() for tier in component.tiers.all()],
        "status": ENGINE_STATUS.get(component.status, component.status),
        "watt": panel.wattage_w if panel is not None else None,
        "kw": _number(spec.kw) if spec is not None else None,
        "type": kind,
        "phase": attributes.get("phase") or (spec.phase if spec is not None else None),
        "gstOverride": percent_of(component.gst_rate_override),
    }
    if spec is not None and spec.device_type:
        item["deviceType"] = spec.device_type
        item["panelsPerDevice"] = spec.panels_per_device
    if attributes.get("micro_accessory_role"):
        item["microAccessoryRole"] = attributes["micro_accessory_role"]
    return {key: value for key, value in item.items() if value is not None}


def flarize_catalog(template: Template, ctx: QtyContext) -> tuple[dict, list[Slot]]:
    from pricing.models import Price, PriceKind

    slots = list(Slot.objects.filter(template=template, category__deleted_at__isnull=True).select_related("category").order_by("sort_order", "id"))
    fixed = list(FixedItem.objects.filter(template=template, is_tube=False).select_related("component").order_by("sort_order", "id"))
    categories = {slot.category.slug: slot.category for slot in slots}
    categories.update({category.slug: category for category in Category.objects.filter(slug__in=ACCESSORY_CATEGORIES)})
    components = list(
        Component.objects.filter(Q(category__in=categories.values()) | Q(pk__in=[row.component_id for row in fixed if row.component_id]))
        .exclude(status=ComponentStatus.RETIRED)
        .select_related("category", "inverter_spec", "panel_spec")
        .prefetch_related("tiers")
        .order_by("created_at", "id")
    )
    prices = dict(Price.objects.filter(kind=PriceKind.LIST, effective_to__isnull=True, component__in=components).values_list("component_id", "amount"))
    sizes = [entry["key"] for entry in template.sizes or []]
    three_phase = set(template.three_phase_sizes or [])
    catalog_categories = {slug: {"label": category.name, "gstDefault": percent_of(category.gst_rate), "items": []} for slug, category in categories.items()}
    for component in components:
        if component.category.slug in catalog_categories:
            catalog_categories[component.category.slug]["items"].append(_item(component, prices.get(component.pk)))
    engine_slots = []
    for slot in slots:
        engine_slot = {"pos": slot.sort_order, "category": slot.category.slug, "label": slot.label, "variable": slot.is_variable, **_table(slot.qty_rule, sizes, three_phase)}
        if slot.gst_rate is not None:
            engine_slot["gst"] = percent_of(slot.gst_rate)
        if slot.filter_type:
            engine_slot["filterType"] = slot.filter_type.lower()
        if slot.filter_phase:
            engine_slot["filterPhase"] = slot.filter_phase
        engine_slots.append(engine_slot)
    engine_fixed = []
    for row in fixed:
        if not matches(row.condition, ctx):
            continue
        price = row.unit_price if row.unit_price is not None else prices.get(row.component_id)
        rule = row.qty_rule if row.qty_rule is not None else {"type": "fixed", "qty": _number(row.qty)}
        engine_fixed.append(
            {
                "id": row.code or (row.component.sku if row.component_id else None),
                "name": row.name,
                "gst": percent_of(row.gst_rate) if row.gst_rate is not None else 18,
                "price": _number(price) or 0,
                **_table(rule, sizes, three_phase),
            }
        )
    engine_template = {
        "sizes": {entry["key"]: entry["label"] for entry in template.sizes or []},
        "tiers": list(template.tiers or ["base", "value", "premium"]),
        "threePhase": list(template.three_phase_sizes or []),
        "batteryConfigs": list(template.battery_configs or []),
        "slots": engine_slots,
        "fixedItems": engine_fixed,
    }
    profiles = {profile.key: _profile(profile) for profile in PackageProfile.objects.select_related("battery_component")}
    return {"categories": catalog_categories, "bomTemplates": {template.system_type.lower(): engine_template}, "packageProfiles": profiles}, slots


def _profile(profile: PackageProfile) -> dict:
    result = {
        "label": profile.label,
        "packageKey": profile.key,
        "structureType": profile.structure_material,
        "inverterType": profile.inverter_type.lower(),
        "batteryIncluded": profile.battery_included,
        "batteryQuantity": profile.battery_quantity,
        "batteryCapacity": _number(profile.battery_capacity_kwh),
    }
    if profile.battery_component_id:
        result["batteryComponentId"] = profile.battery_component.sku
    return result


def _structure(slug: str, kw: float) -> dict | None:
    if not slug:
        return None
    template = StructureTemplate.objects.filter(slug=slug).first()
    if template is None:
        raise validation_error({"structure": [f"Unknown structure template {slug!r}."]})
    lines, tube_kg = [], 0.0
    for item in template.items.order_by("sort_order", "id"):
        qty = evaluate(item.qty_rule, QtyContext(kw=kw))
        if item.item_type == StructureItemType.TUBE:
            tube_kg += float(item.weight_kg or 0) * qty
        lines.append({"name": item.name, "item_type": item.item_type, "qty": qty, "weight_kg": _number(item.weight_kg), "unit_price": _number(item.unit_price)})
    return {"slug": slug, "tube_kg": round(tube_kg, 3), "lines": lines}


def build(*, system_type: str, size: str, tier: str, phase=None, battery_quantity=None, future_system_size: str = "", structure: str = "", selections: dict | None = None) -> dict:
    template = Template.objects.filter(system_type=system_type, is_active=True).first()
    if template is None:
        raise DomainError("bom_template_missing", f"No active BOM template for {system_type}.", errors={"system_type": ["No active template."]})
    system_size = future_system_size or size
    band = str(battery_quantity) if battery_quantity is not None else "0"
    three_phase = phase == "3P" or system_size in (template.three_phase_sizes or [])
    ctx = QtyContext(size_key=system_size, tier=tier.lower(), bat_config=band, kw=_kw(system_size), is_three_phase=three_phase)
    catalog, slots = flarize_catalog(template, ctx)
    config = {"systemType": system_type.lower(), "size": size, "tier": tier.lower(), "selections": selections or {}}
    if phase:
        config["phase"] = phase
    if battery_quantity is not None:
        config["batteryQuantity"] = battery_quantity
    if future_system_size:
        config["futureSystemSize"] = future_system_size
    try:
        result = build_bom(config, catalog=catalog)
    except BomBuildError as exc:
        code = (exc.code or "bom_build_refused").lower()
        raise DomainError(code, exc.message, errors={"detail": [exc.detail]} if exc.detail else None) from None
    built = {line.get("category") for line in result["lines"]}
    warnings = []
    for slot in slots:
        if slot.required and slot.category.slug not in built and evaluate(slot.qty_rule, ctx):
            warnings.append({"code": "slot_unfilled", "slot": slot.key, "message": f"No eligible component with a price for slot {slot.key} ({slot.category.slug})."})
    return {
        "lines": result["lines"],
        "totals": result["totals"],
        "profile": result["profile"],
        "system_config": result["systemConfig"],
        "engineering": result["engineering"],
        "structure": _structure(structure, _kw(system_size)),
        "warnings": warnings,
    }
