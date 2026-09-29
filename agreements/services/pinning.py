"""Values an agreement pins from an issued quotation version (Plan 2 §3.1: "copies the pinned values … Prices are not
typed"), and the technical values of catalog components (blank agreements).

Everything comes from what the quotation froze at issue: the system (type, size, phase, tier, roof) from the version
row, the equipment from the frozen document's locked BOM (``payload.bomSummary.rows`` — panel, inverter or micro
inverters, battery, structure, with quantities and display attributes; ``componentId`` is the catalog SKU), the prices
from the version (customer total incl. GST and transport, offer + approved discounts, final price) and the KSEB fee
from the version's PriceRelease. Nothing is re-priced. Missing equipment is left blank (issuing then reports it).
"""

from __future__ import annotations

from decimal import Decimal

from agreements.models import InverterType, SystemType, Variant
from agreements.services import fees
from agreements.services.common import money

PANEL_ROLES = ("PANEL",)
INVERTER_ROLES = ("INVERTER", "MICRO_INVERTER")
BATTERY_ROLES = ("BATTERY",)
STRUCTURE_ROLES = ("STRUCTURE",)
STRUCTURE_SLUGS = {"flatRoof": "flat_roof", "sheetRoof": "sheet_roof", "elevated": "elevated", "FLAT": "flat_roof", "SHEET": "sheet_roof", "ELEVATED": "elevated"}
PHASE_LABELS = {"1P": "Single Phase", "3P": "3 Phase"}


def _rows(version) -> list[dict]:
    document = version.document_payload or {}
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
    summary = payload.get("bomSummary") if isinstance(payload.get("bomSummary"), dict) else {}
    rows = summary.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _first(rows: list[dict], roles: tuple[str, ...]) -> dict | None:
    return next((row for row in rows if row.get("role") in roles), None)


def _component(row: dict | None):
    from catalog.models import Component

    if row is None or not row.get("componentId"):
        return None
    return Component.objects.filter(sku__iexact=str(row["componentId"])).select_related("brand").first()


def _quantity(row: dict | None) -> int | None:
    try:
        value = int(Decimal(str(row.get("quantity")))) if row else None
    except (ArithmeticError, ValueError, TypeError):
        return None
    return value if value and value > 0 else None


def _attributes(row: dict | None) -> dict:
    attributes = (row or {}).get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _label(brand: str, model: str) -> str:
    brand, model = (brand or "").strip(), (model or "").strip()
    if brand and model and not model.lower().startswith(brand.lower()):
        return f"{brand} - {model}"[:255]
    return (model or brand)[:255]


def size_label(capacity_kw) -> str:
    """``5 KW`` (the legacy page's plant label for a plain size)."""
    if capacity_kw is None:
        return ""
    value = Decimal(str(capacity_kw)).normalize()
    return f"{value:f} KW"


def structure_template(key: str):
    from bom.models import StructureTemplate

    slug = STRUCTURE_SLUGS.get(key or "")
    return StructureTemplate.objects.filter(slug=slug).first() if slug else None


def from_quotation_version(version) -> dict:
    """Column values pinned from ``version`` (an ISSUED ``quotations.Version``)."""
    rows = _rows(version)
    panel_row, inverter_row = _first(rows, PANEL_ROLES), _first(rows, INVERTER_ROLES)
    battery_row, structure_row = _first(rows, BATTERY_ROLES), _first(rows, STRUCTURE_ROLES)
    panel, inverter, battery = _component(panel_row), _component(inverter_row), _component(battery_row)
    panel_attrs, inverter_attrs, battery_attrs = _attributes(panel_row), _attributes(inverter_row), _attributes(battery_row)
    hybrid = version.system_type == "HYBRID"
    watt = panel_attrs.get("moduleWatt")
    if inverter_row is not None and inverter_row.get("role") == "MICRO_INVERTER":
        inverter_type = InverterType.MICRO
    elif inverter_row is not None:
        inverter_type = InverterType.HYBRID if hybrid else InverterType.STRING
    else:
        inverter_type = ""
    total = (version.customer_price_incl_gst or Decimal(0)) + (version.transport_extra or Decimal(0))
    reduction = (version.offer_total or Decimal(0)) + (version.discount_total or Decimal(0))
    if version.final_price is not None:
        final = money(version.final_price)
    elif version.customer_price_incl_gst is not None and total >= reduction:
        final = money(total - reduction)
    else:
        final = None
    original = money(final + reduction) if final is not None else None
    fee = fees.from_release(version.price_release, phase=version.phase, capacity_kw=version.size_kw) if version.price_release_id else None
    fee = fee or fees.current(phase=version.phase, capacity_kw=version.size_kw)
    values = {
        "system_type": SystemType.HYBRID if hybrid else SystemType.ON_GRID,
        "capacity_kw": version.size_kw,
        "size_label": size_label(version.size_kw),
        "phase": version.phase,
        "variant": version.tier if version.tier in Variant.values else "",
        "panel": panel,
        "panel_label": _label(panel_attrs.get("brand") or (panel.brand_label if panel else ""), panel_attrs.get("model") or (panel.name if panel else "")),
        "panel_capacity_w": int(watt) if isinstance(watt, (int, float)) and watt > 0 else None,
        "panel_capacity_label": f"{int(watt)}W" if isinstance(watt, (int, float)) and watt > 0 else "",
        "panel_dcr": panel_attrs.get("dcr") if isinstance(panel_attrs.get("dcr"), bool) else None,
        "panel_qty": _quantity(panel_row),
        "inverter": inverter,
        "inverter_brand": str(inverter_attrs.get("brand") or (inverter.brand_label if inverter else ""))[:100],
        "inverter_type": inverter_type,
        "inverter_qty": _quantity(inverter_row),
        "battery": battery,
        "battery_label": _label(battery_attrs.get("brand") or "", battery_attrs.get("model") or (battery.name if battery else ""))[:64] if battery_row else "",
        "battery_qty": _quantity(battery_row),
        "structure_template": structure_template(version.structure_type) or structure_template(version.roof_type),
        "structure_type": version.roof_type or "",
        "structure_material": str(_attributes(structure_row).get("model") or "")[:120],
        "original_price": original,
        "extra_cost": Decimal("0.00"),
        "discount": money(reduction) or Decimal("0.00"),
        "final_price": final,
        "statutory_fee_id": fee.row_id if fee else None,
        "statutory_fee_label": fee.label if fee else "",
        "statutory_fee_amount": money(fee.amount) if fee else None,
    }
    return values


def component_values(field: str, component) -> dict:
    """Technical columns of a catalog component chosen on a blank agreement (``panel``, ``inverter``, ``battery``)."""
    if component is None:
        return {field: None}
    brand = component.brand_label or (component.brand.name if component.brand_id else "")
    if field == "panel":
        spec = getattr(component, "panel_spec", None)
        watt = spec.wattage_w if spec is not None else None
        return {
            "panel": component,
            "panel_label": _label(brand, component.model or component.name),
            "panel_capacity_w": watt,
            "panel_capacity_label": f"{watt}W" if watt else "",
            "panel_dcr": spec.is_dcr if spec is not None else None,
        }
    if field == "inverter":
        spec = getattr(component, "inverter_spec", None)
        if spec is not None and spec.topology == "MICRO":
            kind = InverterType.MICRO
        elif spec is not None and spec.inverter_type == "HYBRID":
            kind = InverterType.HYBRID
        else:
            kind = InverterType.STRING
        return {"inverter": component, "inverter_brand": brand[:100], "inverter_type": kind}
    spec = getattr(component, "battery_spec", None)
    capacity = spec.capacity_kwh if spec is not None else None
    label = f"{capacity.normalize():f} kWh" if capacity else _label(brand, component.model or component.name)
    return {"battery": component, "battery_label": label[:64]}
