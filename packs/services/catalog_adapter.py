"""The Flarize-shaped catalog the pack engines read, built from the platform tables.

``engines.bom_builder.build_bom`` and ``engines.engineering_checker.check_project_bom`` read the catalog in the shape of
Flarize ``data/catalog.json`` (``categories.<slug>.items[]`` with ``id``, ``name``, ``brand``, ``price``, ``tiers``,
``status``, ``watt``, ``kw``, ``type``, ``phase``, ``gstOverride``, ``deviceType``, ``panelsPerDevice``,
``microAccessoryRole``, the panel/inverter electrical fields) plus ``packageProfiles``, and the checker reads the battery
engineering overlay in the shape of ``data/battery-master.json``. :func:`flarize_catalog` rebuilds both from
``catalog_*`` (specs, attributes, tiers), the prices of a PriceRelease payload (``components.<sku>.list_price``) and the
``bom_package_profile`` rows — the inverse of the catalog importer's Flarize mapping (docs/decisions/catalog.md), so a
catalog imported from Flarize gives the engines exactly the records Flarize gave them (packs parity tests).

Item order inside a category is the Flarize order: components imported from ``catalog.json`` sort by their legacy-map
entry (the importer maps them in file order), platform-created ones after them by ``created_at, id``. The engines pick
"the first eligible item" when nothing else decides, so the order is part of the contract.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Prefetch

from bom.models import PackageProfile
from catalog.models import Category, Component, ComponentStatus, ComponentTier
from core.models import LegacyMap

ENGINE_STATUS = {ComponentStatus.ACTIVE: "ACTIVE", ComponentStatus.DEPRECATED: "ACTIVE", ComponentStatus.DRAFT: "DRAFT", ComponentStatus.RETIRED: "RETIRED"}
FLARIZE_ITEM_TABLE = "catalog.json:items"


def number(value):
    """A ``Decimal`` column as the JavaScript number the engines expect (``int`` when integral)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def percent(fraction):
    if fraction is None:
        return None
    return number(Decimal(fraction) * 100)


def _inverter_type(spec) -> str | None:
    if spec.topology == "MICRO":
        return "micro"
    return {"ONGRID": "ongrid", "HYBRID": "hybrid"}.get(spec.inverter_type or "")


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def item_of(component: Component, price) -> dict:
    """One ``categories.<slug>.items[]`` record."""
    attributes = {key: value for key, value in (component.attributes or {}).items() if not key.startswith("master_")}
    item: dict = {"id": component.sku, "name": component.name}
    if component.brand_label:
        item["brand"] = component.brand_label
    item["tiers"] = [tier.tier.lower() for tier in component.tiers.all()]
    item["price"] = number(price) if price is not None else 0
    item["status"] = ENGINE_STATUS.get(component.status, component.status)
    item["approvalStatus"] = "APPROVED" if component.status in (ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED) else "PENDING"
    if component.model:
        item["model"] = component.model
    if component.gst_rate_override is not None:
        item["gstOverride"] = percent(component.gst_rate_override)
    panel = getattr(component, "panel_spec", None)
    if panel is not None:
        item.update(
            {
                "watt": panel.wattage_w,
                "voc": number(panel.voc_v),
                "isc": number(panel.isc_a),
                "vmp": number(panel.vmp_v),
                "imp": number(panel.imp_a),
                "tempCoeffVoc": number(panel.temperature_coefficient_voc),
                "maxSysVoltage": panel.max_system_voltage_v,
                "cells": panel.cell_count,
            }
        )
        if panel.is_dcr is not None:
            item["dcr"] = panel.is_dcr
    inverter = getattr(component, "inverter_spec", None)
    if inverter is not None:
        item.update(
            {
                "kw": number(inverter.kw),
                "phase": inverter.phase or None,
                "type": _inverter_type(inverter),
                "mpptCount": inverter.mppt_count,
                "maxInputVoltage": inverter.max_pv_voltage_v,
                "maxStringsPerMppt": inverter.max_strings_per_mppt,
                "deviceType": inverter.device_type or None,
                "panelsPerDevice": inverter.panels_per_device,
            }
        )
    for key, value in attributes.items():
        item[_camel(key)] = value
    return {key: value for key, value in item.items() if value is not None}


def _battery_record(component: Component) -> dict | None:
    spec = getattr(component, "battery_spec", None)
    if spec is None:
        return None
    return {
        "componentId": component.sku,
        "brand": component.brand_label or None,
        "model": component.model or None,
        "displayName": component.name,
        "batteryType": spec.battery_type or None,
        "chemistry": spec.chemistry or None,
        "nominalVoltage": number(spec.nominal_voltage_v),
        "minVoltage": number(spec.min_voltage_v),
        "maxVoltage": number(spec.max_voltage_v),
        "capacityKwh": number(spec.capacity_kwh),
        "usableCapacityKwh": number(spec.usable_kwh),
        "continuousChargeCurrent": number(spec.continuous_charge_current_a),
        "continuousDischargeCurrent": number(spec.continuous_discharge_current_a),
        "maximumChargeCurrent": number(spec.maximum_charge_current_a),
        "maximumDischargeCurrent": number(spec.maximum_discharge_current_a),
        "peakCurrent": number(spec.peak_current_a),
        "integratedProtection": spec.integrated_protection,
        "protectionType": spec.protection_type or None,
        "protectionRating": spec.protection_rating or None,
        "externalProtectionRequired": spec.external_protection_required,
        "communicationProtocol": spec.communication_protocol or None,
        "communicationRequired": spec.communication_required,
        "compatibleInverters": spec.compatible_inverters,
        "compatibleSystemTypes": spec.compatible_system_types,
        "compatiblePhases": spec.compatible_phases,
        "architecture": spec.architecture or None,
        "engineeringStatus": spec.engineering_status,
        "procurementStatus": spec.procurement_status,
    }


def _profile(profile: PackageProfile) -> dict:
    result = {
        "label": profile.label,
        "packageKey": profile.key,
        "structureType": profile.structure_material,
        "inverterType": profile.inverter_type.lower(),
        "batteryIncluded": profile.battery_included,
        "batteryQuantity": profile.battery_quantity,
        "batteryCapacity": number(profile.battery_capacity_kwh),
    }
    if profile.battery_component_id:
        result["batteryComponentId"] = profile.battery_component.sku
    return result


def _source_order(components: list[Component]) -> dict[int, int]:
    rows = LegacyMap.objects.filter(source_system=LegacyMap.SourceSystem.FLARIZE, source_table=FLARIZE_ITEM_TABLE, target_table="catalog_component", target_id__in=[c.pk for c in components])
    return dict(rows.values_list("target_id", "id"))


def component_queryset():
    return Component.objects.select_related("category", "panel_spec", "inverter_spec", "battery_spec").prefetch_related(Prefetch("tiers", queryset=ComponentTier.objects.order_by("id")))


def flarize_catalog(prices: dict[str, object]) -> dict:
    """``{"categories", "packageProfiles", "batteryMaster"}`` from the live catalog; ``prices`` maps SKU → list price.

    Every live category (retired components included, with ``status`` RETIRED: the engines refuse them, the checker
    names them) — templates may name any category and the premium step reads ``enphase``/``ug_cable``.
    """
    categories = list(Category.objects.order_by("sort_order", "id"))
    components = list(component_queryset().order_by("created_at", "id"))
    order = _source_order(components)
    components.sort(key=lambda c: (0, order[c.pk]) if c.pk in order else (1, c.created_at, c.pk))
    catalog = {category.slug: {"label": category.name, "gstDefault": percent(category.gst_rate), "items": []} for category in categories}
    slugs = {category.pk: category.slug for category in categories}
    battery_master = {}
    for component in components:
        slug = slugs.get(component.category_id)
        if slug is None:
            continue
        catalog[slug]["items"].append(item_of(component, prices.get(component.sku)))
        record = _battery_record(component)
        if record is not None:
            battery_master[component.sku] = record
    profiles = {profile.key: _profile(profile) for profile in PackageProfile.objects.select_related("battery_component").order_by("id")}
    return {"categories": catalog, "packageProfiles": profiles, "batteryMaster": battery_master}
