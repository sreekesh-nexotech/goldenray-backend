"""The website quote engine — the legacy ``BomCalculator`` + ``BomCalculateView`` (main backend
``bom/calculator.py``, ``bom/views/api.py``) on the platform tables.

``quote(data, today=…)`` validates and coerces the request exactly as the legacy view did (``_validate`` /
``_coerce``), reads the configuration from the new tables and returns the legacy response body (``bom_lines``,
``cost_breakdown``, ``totals``, ``pricing``, ``meta``, ``available_offers``) with the same keys, key order and number
types — replayed against 1,077 captured legacy responses in ``bom/tests/test_quote_parity.py``.

Where the numbers come from (legacy model → platform table):

* ``GlobalCosts`` → ``pricing_cost_config`` current rows (``transport_rate_per_km``, ``transport_base_km``,
  ``office_per_project``, ``structure_repair_pct`` as a fraction …); the calculator's constant 35 ₹ per extra km is
  ``transport_extra_rate_per_km`` (35 when the key is not configured);
* ``BomTemplate`` / ``BomSlot`` / ``BomFixedItem`` → ``bom_template`` / ``bom_slot`` (``qty_rule``) / ``bom_fixed_item``;
* ``Category.items`` + ``ItemTier`` → live, non-retired ``catalog_component`` rows of the slot's category (oldest first,
  as the legacy table's id order) with their ``catalog_component_tier`` rows; ``item.price`` → the current LIST
  ``pricing_price``; ``item.brand`` → ``brand_label``; ``item.phase`` / ``inverter_type`` / ``kw`` → the inverter spec
  or ``attributes.phase`` / ``attributes.type`` as the catalog import stored them;
* ``StructureTemplate`` (+ items) → ``bom_structure_template`` (+ items, ``kw_interpolated`` rules);
* ``MarketRate`` → the ACTIVE ``pricing_market_rate_set``'s cells; ``Offer`` → ACTIVE ``pricing_offer`` rows.

Arithmetic runs in binary floating point in the legacy operation order (DV-17's rule for parity ports): the published
legacy numbers depend on it. The service takes ``today`` explicitly (the legacy code filtered offers by date).

Differences from the legacy endpoint (approved, docs/decisions/bom.md): inputs the legacy view crashed on (HTTP 500 —
``custom_discount`` sent as a number, non-numeric ``ghs_houses``/upgrade sizes, a non-object body …) are a 400
``validation_error``; a missing template is 400 ``bom_template_missing``; missing cost configuration is 503
``quote_not_configured`` (the legacy view seeded its database from a JSON file instead).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q

from bom.models import FixedItem, Slot, StructureItemType, StructureTemplate, Template
from bom.services.common import percent_of
from bom.services.qty_rules import QtyContext, evaluate, matches, new_panels
from catalog.models import Component, ComponentStatus
from catalog.models.category import BomRole, Unit
from core.errors import DomainError

VALID_SYS_TYPES = ("ongrid", "hybrid", "upgrade")
VALID_TIERS = ("base", "value", "premium")
VALID_SUBSIDY = ("residential", "ghs", "none")
VALID_MARGIN = ("percent", "flat")
VALID_STRUCTURE = ("flatRoof", "elevated", "sheetRoof")
STRUCTURE_SLUGS = {"flatRoof": "flat_roof", "elevated": "elevated", "sheetRoof": "sheet_roof"}
DEFAULT_SECTIONS = {"panels": True, "structure": True, "inverter": True, "hybrid_inv": False, "battery": False, "wiring": True}
MAX_MAGNITUDE = 1e12  # numbers beyond this made the legacy arithmetic overflow or lose every digit

# Category-to-upgrade-section mapping (legacy UPG_SECTION_MAP, mirrors upgSectionMap in App.jsx).
UPG_SECTION_MAP = {
    "panel": "panels",
    "solar_clamp": "structure",
    "ss_screw": "structure",
    "structure_material": "structure",
    "painting_material": "structure",
    "welding_material": "structure",
    "inverter": "inverter",
    "dcdb": "inverter",
    "acdb": "inverter",
    "battery": "battery",
    "mccb_box": "battery",
    "change_over": "battery",
    "battery_cable": "battery",
    "dc_cable": "wiring",
    "ac_cable": "wiring",
    "armoured_cable": "wiring",
    "mc4_connector": "wiring",
    "cable_tie": "wiring",
    "copper_lug": "wiring",
    "insulation_tape": "wiring",
    "conduit_pipe": "wiring",
    "meter": "inverter",
    "ug_cable": "wiring",
}
COST_KEYS = {
    "install_rate": "install_rate",
    "service_rate_year": "service_rate_year",
    "service_years": "service_years",
    "transport_rate": "transport_rate_per_km",
    "default_dist_km": "transport_base_km",
    "miscellaneous": "miscellaneous",
    "office": "office_per_project",
    "gp_rate_per_kg": "gp_rate_per_kg",
    "gi_rate_per_kg": "gi_rate_per_kg",
    "structure_labor": "structure_labor",
    "structure_repair_pct": "structure_repair_pct",
}
LEGACY_EXTRA_KM_RATE = 35
INVERTER_TYPES = {"ONGRID": "ongrid", "HYBRID": "hybrid"}
QUOTABLE = (ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED)  # never a DRAFT or RETIRED component on a website quote


class QuoteInvalid(DomainError):
    """400 ``validation_error``; ``legacy_errors`` is the legacy ``{"errors": [...]}`` list for the ``/legacy/`` shim."""

    status = 400

    def __init__(self, messages: list[str], errors: dict):
        self.legacy_errors = list(messages)
        super().__init__("validation_error", "; ".join(messages), errors=errors)


# ── request (legacy _validate / _coerce) ─────────────────────────────────────────────────────────────────────


def _hashable_in(value, allowed) -> bool:
    return isinstance(value, str) and value in allowed


def _validate(data: dict) -> tuple[list[str], dict]:
    """The legacy ``_validate`` messages, in the legacy order, with the field each one concerns."""
    messages: list[tuple[str, str]] = []
    if not _hashable_in(data.get("sys_type"), VALID_SYS_TYPES):
        messages.append(("sys_type", "sys_type must be ongrid | hybrid | upgrade"))
    if not _hashable_in(data.get("tier"), VALID_TIERS):
        messages.append(("tier", "tier must be base | value | premium"))
    if not data.get("size"):
        messages.append(("size", "size is required"))
    if not _hashable_in(data.get("subsidy_type", "none"), VALID_SUBSIDY):
        messages.append(("subsidy_type", "subsidy_type must be residential | ghs | none"))
    if not _hashable_in(data.get("margin_type", "percent"), VALID_MARGIN):
        messages.append(("margin_type", "margin_type must be percent | flat"))
    if not _hashable_in(data.get("structure_type", "flatRoof"), VALID_STRUCTURE):
        messages.append(("structure_type", "structure_type must be flatRoof | elevated | sheetRoof"))
    for name in ("dist_km", "margin_val"):
        try:
            float(data.get(name, 0))
        except (TypeError, ValueError, OverflowError):
            messages.append((name, f"{name} must be a number"))
    return [message for _, message in messages], _by_field(messages)


def _by_field(messages: list[tuple[str, str]]) -> dict:
    errors: dict = {}
    for name, message in messages:
        errors.setdefault(name, []).append(message)
    return errors


def _finite(value: float, name: str, problems: list) -> float:
    if not math.isfinite(value) or abs(value) > MAX_MAGNITUDE:
        problems.append((name, f"{name} must be a finite number of at most {MAX_MAGNITUDE:.0f}"))
    return value


def _coerce(data: dict) -> dict:
    """The legacy ``_coerce``, raising :class:`QuoteInvalid` where the legacy code raised (HTTP 500 there)."""
    problems: list[tuple[str, str]] = []

    def number(name: str, default, convert):
        try:
            return convert(data.get(name, default))
        except (TypeError, ValueError, OverflowError):
            problems.append((name, f"{name} must be a {'whole number' if convert is int else 'number'}"))
            return convert(default)

    dist_km = number("dist_km", 100, int)
    ghs_houses = number("ghs_houses", 1, int)
    margin_val = _finite(number("margin_val", 20, float), "margin_val", problems)
    from_kw = _finite(number("upgrade_from_kw", 3, float), "upgrade_from_kw", problems)
    to_kw = _finite(number("upgrade_to_kw", 5, float), "upgrade_to_kw", problems)
    _finite(float(dist_km), "dist_km", problems)
    _finite(float(ghs_houses), "ghs_houses", problems)
    custom = data.get("custom_discount") or None
    if custom is not None:
        if not isinstance(custom, dict):
            problems.append(("custom_discount", 'custom_discount must be an object {"type": "percent" | "flat", "value": <number>}'))
            custom = None
        elif custom.get("value"):
            try:
                _finite(float(custom["value"]), "custom_discount", problems)
            except (TypeError, ValueError, OverflowError):
                problems.append(("custom_discount", "custom_discount.value must be a number"))
    sections = data.get("upgrade_sections") or dict(DEFAULT_SECTIONS)
    if data["sys_type"] == "upgrade" and not isinstance(sections, dict):
        problems.append(("upgrade_sections", "upgrade_sections must be an object"))
    offer_id = data.get("selected_offer_id") or None
    if offer_id is not None and not isinstance(offer_id, (str, int, float)):
        problems.append(("selected_offer_id", "selected_offer_id must be a string"))
    if problems:
        raise QuoteInvalid([message for _, message in problems], _by_field(problems))
    return {
        "sys_type": data["sys_type"],
        "size": str(data["size"]),
        "tier": data["tier"],
        "bat_config": str(data.get("bat_config", "0")),
        "mode": data.get("mode", "quotation"),
        "dist_km": max(0, dist_km),
        "structure_type": data.get("structure_type", "flatRoof"),
        "subsidy_type": data.get("subsidy_type", "none"),
        "ghs_houses": max(1, ghs_houses),
        "selected_offer_id": offer_id,
        "custom_discount": custom,
        "margin_type": data.get("margin_type", "percent"),
        "margin_val": margin_val,
        "upgrade_from_kw": from_kw,
        "upgrade_to_kw": to_kw,
        "upgrade_sections": sections,
    }


def parse_request(data) -> dict:
    if not isinstance(data, dict):
        raise QuoteInvalid(["The request body must be a JSON object"], {"non_field_errors": ["The request body must be a JSON object."]})
    messages, errors = _validate(data)
    if messages:
        raise QuoteInvalid(messages, errors)
    return _coerce(data)


# ── the configuration snapshot (read once per quote) ─────────────────────────────────────────────────────────


@dataclass
class Item:
    name: str
    brand: str
    price: Decimal
    phase: str | None
    inverter_type: str | None
    kw: Decimal | None
    tiers: frozenset


@dataclass
class CategoryView:
    slug: str
    gst_default: int | float
    unit_m: bool
    is_inverter: bool
    items: list[Item] = field(default_factory=list)


@dataclass
class SlotView:
    pos: int
    variable: bool
    gst: int | float | None
    category: CategoryView
    filter_type: str
    filter_phase: str
    rule: dict


@dataclass
class FixedView:
    name: str
    price: Decimal | None
    item: Item | None
    category_slug: str
    category_gst: int | float | None
    gst: int | float | None
    rule: dict
    condition: dict
    section: str
    is_tube: bool


@dataclass
class StructureItemView:
    item_type: str
    weight_kg: Decimal | None
    price: Decimal | None
    rule: dict


@dataclass
class Snapshot:
    costs: dict
    three_phase_sizes: list
    slots: list[SlotView]
    fixed_items: list[FixedView]
    structures: dict[str, list[StructureItemView]]
    market_rates: list[dict]
    offers: list[dict]


def _costs() -> dict:
    from pricing.services.cost_config import current_values

    values = current_values()
    missing = [key for key in COST_KEYS.values() if key not in values]
    if missing:
        raise DomainError("quote_not_configured", f"The quote engine needs the cost configuration keys {', '.join(missing)}.", status=503, errors={"cost_config": missing})
    costs = {name: values[key] for name, key in COST_KEYS.items()}
    costs["structure_repair_pct"] = float(Decimal(str(costs["structure_repair_pct"])) * 100)
    costs["extra_km_rate"] = values.get("transport_extra_rate_per_km", LEGACY_EXTRA_KM_RATE)
    return costs


def _legacy_item(component: Component, price: Decimal) -> Item:
    spec = getattr(component, "inverter_spec", None)
    attributes = component.attributes or {}
    phase = attributes.get("phase") or (spec.phase if spec is not None else None) or None
    if spec is not None:
        inverter_type = "micro" if spec.topology == "MICRO" else INVERTER_TYPES.get(spec.inverter_type)
    else:
        inverter_type = attributes.get("type") or None
    return Item(
        name=component.name,
        brand=component.brand_label,
        price=price,
        phase=phase,
        inverter_type=inverter_type,
        kw=spec.kw if spec is not None else None,
        tiers=frozenset(tier.tier.lower() for tier in component.tiers.all()),
    )


def _items(category_ids: set[int], extra_component_ids: set[int]) -> tuple[dict[int, list[Item]], dict[int, Item]]:
    from pricing.models import Price, PriceKind

    components = list(
        Component.objects.filter(Q(category_id__in=category_ids) | Q(pk__in=extra_component_ids), status__in=QUOTABLE)
        .select_related("inverter_spec")
        .prefetch_related("tiers")
        .order_by("created_at", "id")
    )
    prices = dict(Price.objects.filter(kind=PriceKind.LIST, effective_to__isnull=True, component_id__in=[c.pk for c in components]).values_list("component_id", "amount"))
    by_category: dict[int, list[Item]] = {}
    by_id: dict[int, Item] = {}
    for component in components:
        price = prices.get(component.pk)
        if price is None:
            continue  # nothing to quote without a current LIST price
        item = _legacy_item(component, price)
        by_id[component.pk] = item
        if component.category_id in category_ids:
            by_category.setdefault(component.category_id, []).append(item)
    return by_category, by_id


def load_snapshot(sys_type: str) -> Snapshot:
    costs = _costs()
    template = Template.objects.filter(system_type=sys_type.upper(), is_active=True).first()
    if template is None:
        raise DomainError("bom_template_missing", f"No BOM template found for system type '{sys_type}'.", status=400, errors={"sys_type": [f"No BOM template for {sys_type}."]})
    slots = list(Slot.objects.filter(template=template, category__deleted_at__isnull=True).select_related("category").order_by("sort_order", "id"))
    fixed = list(FixedItem.objects.filter(template=template).select_related("category", "component").order_by("sort_order", "id"))
    category_ids = {slot.category_id for slot in slots}
    items_by_category, items_by_id = _items(category_ids, {row.component_id for row in fixed if row.component_id})
    categories: dict[int, CategoryView] = {}
    for slot in slots:
        category = slot.category
        if category.pk not in categories:
            categories[category.pk] = CategoryView(
                slug=category.slug,
                gst_default=percent_of(category.gst_rate),
                unit_m=category.unit == Unit.M,
                is_inverter=category.bom_role == BomRole.MAIN_INVERTER,
                items=items_by_category.get(category.pk, []),
            )
    slot_views = [
        SlotView(
            pos=slot.sort_order,
            variable=slot.is_variable,
            gst=percent_of(slot.gst_rate),
            category=categories[slot.category_id],
            filter_type=slot.filter_type.lower(),
            filter_phase=slot.filter_phase,
            rule=slot.qty_rule,
        )
        for slot in slots
    ]
    fixed_views = []
    for row in fixed:
        category = row.category if row.category is not None and row.category.deleted_at is None else None
        fixed_views.append(
            FixedView(
                name=row.name,
                price=row.unit_price,
                item=items_by_id.get(row.component_id) if row.component_id else None,
                category_slug=category.slug if category else "",
                category_gst=percent_of(category.gst_rate) if category else None,
                gst=percent_of(row.gst_rate),
                rule=row.qty_rule if row.qty_rule is not None else {"type": "fixed", "qty": _plain(row.qty)},
                condition=row.condition or {},
                section=row.section,
                is_tube=row.is_tube,
            )
        )
    return Snapshot(
        costs=costs,
        three_phase_sizes=list(template.three_phase_sizes or []),
        slots=slot_views,
        fixed_items=fixed_views,
        structures=_structures() if sys_type != "upgrade" else {},
        market_rates=_market_rates(sys_type),
        offers=_offers(),
    )


def _plain(number: Decimal | None):
    if number is None:
        return 0
    return int(number) if number == number.to_integral_value() else float(number)


def _structures() -> dict[str, list[StructureItemView]]:
    result: dict[str, list[StructureItemView]] = {}
    for template in StructureTemplate.objects.prefetch_related("items"):
        items = sorted(template.items.all(), key=lambda item: (item.sort_order, item.pk))
        result[template.slug] = [StructureItemView(item_type=item.item_type, weight_kg=item.weight_kg, price=item.unit_price, rule=item.qty_rule) for item in items]
    return result


def _market_rates(sys_type: str) -> list[dict]:
    from pricing.models import MarketRate, MarketRateSetStatus

    rows = MarketRate.objects.filter(set__status=MarketRateSetStatus.ACTIVE, set__deleted_at__isnull=True, system_type=sys_type.upper(), future_size_key="", variant="").order_by("sort_order", "id")
    return [
        {"tier": row.tier.lower(), "bat_config": row.battery_config, "size_key": row.size_key, "from_size": row.from_size_key, "value": row.customer_price_incl_gst}
        for row in rows.only("tier", "battery_config", "size_key", "from_size_key", "customer_price_incl_gst")
    ]


def _offers() -> list[dict]:
    from pricing.models import Offer, OfferStatus

    rows = Offer.objects.filter(status=OfferStatus.ACTIVE).order_by("created_at", "id")
    return [
        {
            "offer_id": row.code,
            "name": row.name,
            "offer_type": {"FLAT": "flat", "PERCENT": "percent"}[row.type],
            "value": row.value,
            "applies_to": (row.applies_to_system or "ALL").lower(),
            "applies_to_tier": (row.applies_to_tier or "ALL").lower(),
            "applies_to_size": row.applies_to_size_key,
            "start_date": row.starts_on,
            "end_date": row.ends_on,
        }
        for row in rows
    ]


# ── the calculator (legacy BomCalculator, operation for operation) ───────────────────────────────────────────


class Calculator:
    def __init__(self, snapshot: Snapshot, params: dict, today: date):
        self.s = snapshot
        self.costs = snapshot.costs
        self.p = params
        self.today = today
        # The view's filter (active + dates), then the size target the platform offers add.
        self.offers = [o for o in snapshot.offers if not (o["start_date"] and o["start_date"] > today) and not (o["end_date"] and o["end_date"] < today)]

    def compute(self) -> dict:
        if self.p["sys_type"] == "upgrade":
            return self._compute_upgrade()
        return self._compute_standard()

    def _cost(self, name: str) -> float:
        return float(self.costs[name])

    # ── standard BOM (ongrid / hybrid) ──

    def _compute_standard(self) -> dict:
        p = self.p
        size = p["size"]
        tier = p["tier"]
        bat_config = p["bat_config"]
        is_three_phase = size in (self.s.three_phase_sizes or [])
        bom_lines = self._get_slot_lines(size, tier, bat_config, is_three_phase)
        bom_lines += self._get_fixed_lines(size, tier, bat_config, is_three_phase)
        totals = self._calc_totals(bom_lines)
        kw = self._size_to_kw(size)
        cost_bd = self._calc_costs(totals["material_with_gst"], kw)
        subtotal = totals["material_with_gst"] + cost_bd["cost_total"]
        margin = self._calc_margin(subtotal)
        grand_total = subtotal + margin
        market_rate = self._lookup_market_rate()
        customer_price = market_rate if market_rate > 0 else grand_total
        subsidy = self._calc_subsidy(kw)
        discount_info = self._calc_discount(customer_price)
        final_price = max(0, discount_info["discount_base"] - discount_info["discount_amt"])
        price_after_subsidy = max(0, final_price - subsidy)
        available_offers = self._applicable_offers()
        return {
            "bom_lines": bom_lines,
            "cost_breakdown": cost_bd,
            "totals": {**totals, "subtotal": subtotal, "margin": margin, "grand_total": grand_total},
            "pricing": {
                "market_rate": market_rate,
                "customer_price": customer_price,
                "discount_amt": discount_info["discount_amt"],
                "discount_label": discount_info["discount_label"],
                "final_price": final_price,
                "subsidy_amt": subsidy,
                "subsidy_label": self._subsidy_label(kw),
                "price_after_subsidy": price_after_subsidy,
            },
            "meta": {"system_type": p["sys_type"], "size": size, "tier": tier, "bat_config": bat_config, "is_three_phase": is_three_phase, "new_panels": None},
            "available_offers": available_offers,
        }

    def _context(self, size, tier, bat_config, is_three_phase) -> QtyContext:
        return QtyContext(size_key=size, tier=tier, bat_config=bat_config, kw=self._size_to_kw(size), is_three_phase=is_three_phase)

    def _get_slot_lines(self, size, tier, bat_config, is_three_phase) -> list:
        lines = []
        ctx = self._context(size, tier, bat_config, is_three_phase)
        for slot in self.s.slots:
            qty = evaluate(slot.rule, ctx)
            if not qty:
                continue
            cat = slot.category
            item = self._select_item(cat, tier, slot, is_three_phase, self._size_to_kw(size))
            if not item:
                continue
            unit_price = float(item.price)
            amount = qty * unit_price
            gst_pct = slot.gst if slot.gst is not None else cat.gst_default
            gst_amt = round(amount * gst_pct / 100)
            lines.append(
                {
                    "pos": slot.pos,
                    "name": item.name,
                    "brand": item.brand,
                    "qty": qty,
                    "unit": "m" if cat.unit_m else "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": gst_amt,
                    "section": cat.slug,
                    "is_variable": slot.variable,
                }
            )
        return lines

    def _get_fixed_lines(self, size, tier, bat_config, is_three_phase) -> list:
        lines = []
        pos_offset = 500
        ctx = self._context(size, tier, bat_config, is_three_phase)
        for fi in self.s.fixed_items:
            if fi.is_tube:
                continue  # tube items are priced through the structure cost, not as BOM lines
            if not matches(fi.condition, ctx):
                continue
            qty = evaluate(fi.rule, ctx)
            if not qty:
                continue
            price = fi.price if fi.price is not None else (fi.item.price if fi.item else None)
            if price is None:
                continue
            unit_price = float(price)
            amount = qty * unit_price
            gst_pct = self._fixed_gst(fi)
            gst_amt = round(amount * gst_pct / 100)
            lines.append(
                {
                    "pos": pos_offset,
                    "name": fi.name,
                    "brand": fi.item.brand if fi.item else "",
                    "qty": qty,
                    "unit": "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": gst_amt,
                    "section": fi.section or (fi.category_slug or "misc"),
                    "is_variable": False,
                }
            )
            pos_offset += 1
        return lines

    @staticmethod
    def _fixed_gst(fi: FixedView):
        if fi.gst is not None:
            return fi.gst
        return fi.category_gst if fi.category_gst is not None else 18

    def _select_item(self, category: CategoryView, tier, slot: SlotView, is_three_phase, kw_num):
        available = []
        for item in category.items:
            if tier not in item.tiers:
                continue
            ft = slot.filter_type
            if ft == "hybrid":
                if item.inverter_type != "hybrid":
                    continue
            elif ft == "ongrid":
                if item.inverter_type and item.inverter_type != "ongrid":
                    continue
            fp = slot.filter_phase
            if fp == "HYB":
                if not item.phase or "HYB" not in item.phase:
                    continue
                if is_three_phase and "3P" not in item.phase:
                    continue
                if not is_three_phase and "1P" not in item.phase:
                    continue
            elif item.phase:
                if is_three_phase:
                    if "3P" not in item.phase:
                        continue
                elif "3P" in item.phase:
                    continue
            available.append(item)
        if not available:
            return None
        if category.is_inverter and any(i.kw for i in available):
            return min(available, key=lambda i: abs(float(i.kw or 0) - kw_num))
        return available[0]

    # ── structure cost ──

    def _calc_structure_cost(self, source_slug: str, kw: float) -> dict:
        items = self.s.structures.get(STRUCTURE_SLUGS.get(source_slug, source_slug))
        if items is None:
            return {"total": 0, "lines": []}
        tube_rate = self._cost("gp_rate_per_kg") if self.p["tier"] == "base" else self._cost("gi_rate_per_kg")
        total = 0
        ctx = QtyContext(kw=kw)
        for item in items:
            qty = evaluate(item.rule, ctx)
            if item.item_type == StructureItemType.TUBE:
                weight_kg = float(item.weight_kg or 0)
                cost = round(weight_kg * tube_rate * qty)
            else:
                cost = round(float(item.price or 0) * qty)
            total += cost
        return {"total": total}

    def _calc_costs(self, mat_with_gst: float, kw: float) -> dict:
        p = self.p
        dist_km = p["dist_km"]
        struct_type = p["structure_type"]
        install = self._cost("install_rate") * kw
        service = self._cost("service_rate_year") * self._cost("service_years")
        transport = dist_km * self._cost("transport_rate")
        extra_km = max(0, dist_km - int(self.costs["default_dist_km"]))
        transport += extra_km * self.costs["extra_km_rate"]
        misc = self._cost("miscellaneous")
        office = self._cost("office")
        structure_base = self._calc_structure_cost("flatRoof", kw)["total"]
        structure_extra = 0
        structure_labor = 0
        structure_repair = 0
        if struct_type != "flatRoof":
            selected = self._calc_structure_cost(struct_type, kw)
            structure_extra = max(0, selected["total"] - structure_base)
            structure_labor = self._cost("structure_labor")
            structure_repair = round((structure_extra + structure_labor) * float(self.costs["structure_repair_pct"]) / 100)
        cost_total = install + service + transport + misc + office + structure_base + structure_extra + structure_labor + structure_repair
        return {
            "install": round(install),
            "service": round(service),
            "transport": round(transport),
            "misc": round(misc),
            "office": round(office),
            "structure_base": structure_base,
            "structure_extra": structure_extra,
            "structure_labor": round(structure_labor),
            "structure_repair": structure_repair,
            "cost_total": round(cost_total),
        }

    @staticmethod
    def _calc_totals(lines: list) -> dict:
        mat_ex_gst = sum(line["amount"] for line in lines)
        gst_5 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 5)
        gst_12 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 12)
        gst_18 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 18)
        mat_with_gst = mat_ex_gst + gst_5 + gst_12 + gst_18
        return {"material_ex_gst": round(mat_ex_gst), "gst_5": round(gst_5), "gst_12": round(gst_12), "gst_18": round(gst_18), "material_with_gst": round(mat_with_gst)}

    def _calc_margin(self, subtotal: float) -> float:
        if self.p["margin_type"] == "percent":
            return round(subtotal * self.p["margin_val"] / 100)
        return round(self.p["margin_val"])

    # ── market rate ──

    @staticmethod
    def _rate(value: Decimal):
        return float(value) if value else 0

    def _lookup_market_rate(self) -> float:
        p = self.p
        sys_type, tier, bat_config, size = p["sys_type"], p["tier"], p["bat_config"], p["size"]
        for row in self.s.market_rates:
            if row["tier"] != tier or row["size_key"] != size:
                continue
            if sys_type == "ongrid" and not row["bat_config"] and not row["from_size"]:
                return self._rate(row["value"])
            if sys_type == "hybrid" and row["bat_config"] == bat_config:
                return self._rate(row["value"])
        return 0

    def _lookup_upgrade_market_rate(self, from_kw: float, to_kw: float) -> float:
        from_str = str(int(round(from_kw)))
        to_str = str(int(round(to_kw)))
        for row in self.s.market_rates:
            if row["from_size"] == from_str and row["size_key"] == to_str:
                return self._rate(row["value"])
        return 0

    # ── subsidy ──

    @staticmethod
    def _size_to_kw(size: str) -> float:
        text = size.replace("sp", "").replace("tp", "")
        try:
            return float(text)
        except ValueError:
            return 3.0

    def _calc_subsidy(self, kw: float) -> float:
        p = self.p
        kind = p["subsidy_type"]
        if kind == "none":
            return 0
        if kind == "residential":
            if kw <= 2:
                return kw * 30000
            if kw <= 3:
                return 60000 + (kw - 2) * 18000
            return 78000
        if kind == "ghs":
            houses = max(1, int(p.get("ghs_houses", 1)))
            per_house_kw = min(kw, 3)
            raw = per_house_kw * 18000 * houses
            cap = min(500, houses * 3) * 18000
            return min(raw, cap)
        return 0

    def _subsidy_label(self, kw: float) -> str:
        kind = self.p["subsidy_type"]
        if kind == "residential":
            return f"PM Surya Ghar (Residential, {kw}kW)"
        if kind == "ghs":
            return f"PM Surya Ghar (GHS, {self.p.get('ghs_houses', 1)} houses)"
        return ""

    # ── discount / offers ──

    def _applicable_offers(self) -> list:
        p = self.p
        result = []
        for o in self.offers:
            if o["applies_to"] not in ("all", p["sys_type"]):
                continue
            if o["applies_to_tier"] not in ("all", p["tier"]):
                continue
            if o["applies_to_size"] and o["applies_to_size"] != p["size"]:
                continue
            result.append({"offer_id": o["offer_id"], "name": o["name"], "offer_type": o["offer_type"], "value": float(o["value"])})
        return result

    def _calc_discount(self, base_price: float) -> dict:
        p = self.p
        offer_id = p.get("selected_offer_id")
        custom = p.get("custom_discount")
        if offer_id:
            match = next((o for o in self._applicable_offers() if o["offer_id"] == offer_id), None)
            if match:
                if match["offer_type"] == "percent":
                    amount = round(base_price * match["value"] / 100)
                else:
                    amount = round(float(match["value"]))
                return {"discount_amt": amount, "discount_label": match["name"], "discount_base": base_price}
        if custom and custom.get("value"):
            value = float(custom["value"])
            if custom.get("type") == "percent":
                amount = round(base_price * value / 100)
                label = f"Custom {value}% discount"
            else:
                amount = round(value)
                label = f"Custom ₹{amount:,} discount"
            return {"discount_amt": amount, "discount_label": label, "discount_base": base_price}
        return {"discount_amt": 0, "discount_label": "", "discount_base": base_price}

    # ── upgrade BOM ──

    def _compute_upgrade(self) -> dict:
        p = self.p
        from_kw = float(p.get("upgrade_from_kw", 3))
        to_kw = float(p.get("upgrade_to_kw", 5))
        tier = p["tier"]
        sections = p.get("upgrade_sections", DEFAULT_SECTIONS)
        panels = new_panels(from_kw, to_kw)
        is_hyb_upgrade = sections.get("hybrid_inv", False)
        size_key = str(int(to_kw)) if to_kw == int(to_kw) else str(to_kw)
        ctx = QtyContext(size_key=size_key, tier=tier, bat_config="0", kw=to_kw, is_three_phase=to_kw >= 5, panels=panels, upgrade=True, upgrade_from_kw=from_kw, upgrade_to_kw=to_kw)
        lines = []
        pos = 0
        for slot in self.s.slots:
            cat = slot.category
            cat_slug = cat.slug
            section = UPG_SECTION_MAP.get(cat_slug, "panels")
            effective_section = ("hybrid_inv" if is_hyb_upgrade else "inverter") if cat_slug == "inverter" else section
            if not sections.get(effective_section, False):
                continue
            qty = evaluate(slot.rule, ctx)
            if not qty:
                continue
            is_3p = to_kw >= 5 and cat_slug in ("inverter", "acdb")
            item = self._select_item_upgrade(cat, tier, slot, is_3p, to_kw)
            if not item:
                continue
            unit_price = float(item.price)
            amount = qty * unit_price
            gst_pct = slot.gst or (cat.gst_default if cat else 18)
            gst_amt = round(amount * gst_pct / 100)
            lines.append(
                {
                    "pos": pos,
                    "name": item.name,
                    "brand": item.brand,
                    "qty": qty,
                    "unit": "m" if cat.unit_m else "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": gst_amt,
                    "section": section,
                    "is_variable": True,
                }
            )
            pos += 1
        for fi in self.s.fixed_items:
            if fi.is_tube:
                continue
            section = fi.section or UPG_SECTION_MAP.get(fi.category_slug, "panels")
            if not sections.get(section, False):
                continue
            item = fi.item
            if not item:
                continue
            if not matches(fi.condition, ctx):
                continue
            qty = evaluate(fi.rule, ctx)
            if not qty:
                continue
            unit_price = float(fi.price if fi.price is not None else item.price)
            amount = qty * unit_price
            gst_pct = self._fixed_gst(fi)
            gst_amt = round(amount * gst_pct / 100)
            lines.append(
                {
                    "pos": pos,
                    "name": item.name,
                    "brand": item.brand,
                    "qty": qty,
                    "unit": "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": gst_amt,
                    "section": section,
                    "is_variable": False,
                }
            )
            pos += 1
        totals = self._calc_totals(lines)
        install = self._cost("install_rate") * (to_kw - from_kw)
        transport = p["dist_km"] * self._cost("transport_rate")
        misc = self._cost("miscellaneous")
        office = self._cost("office")
        cost_total = round(install + transport + misc + office)
        subtotal = totals["material_with_gst"] + cost_total
        margin = self._calc_margin(subtotal)
        grand_total = subtotal + margin
        market_rate = self._lookup_upgrade_market_rate(from_kw, to_kw)
        customer_price = market_rate if market_rate > 0 else grand_total
        subsidy = self._calc_subsidy(to_kw)
        discount_info = self._calc_discount(customer_price)
        final_price = max(0, discount_info["discount_base"] - discount_info["discount_amt"])
        price_after_subsidy = max(0, final_price - subsidy)
        cost_bd = {
            "install": round(install),
            "service": 0,
            "transport": round(transport),
            "misc": round(misc),
            "office": round(office),
            "structure_base": 0,
            "structure_extra": 0,
            "structure_labor": 0,
            "structure_repair": 0,
            "cost_total": cost_total,
        }
        return {
            "bom_lines": lines,
            "cost_breakdown": cost_bd,
            "totals": {**totals, "subtotal": subtotal, "margin": margin, "grand_total": grand_total},
            "pricing": {
                "market_rate": market_rate,
                "customer_price": customer_price,
                "discount_amt": discount_info["discount_amt"],
                "discount_label": discount_info["discount_label"],
                "final_price": final_price,
                "subsidy_amt": subsidy,
                "subsidy_label": self._subsidy_label(to_kw),
                "price_after_subsidy": price_after_subsidy,
            },
            "meta": {"system_type": "upgrade", "size": f"{from_kw}→{to_kw}", "tier": tier, "bat_config": "0", "is_three_phase": to_kw >= 5, "new_panels": panels},
            "available_offers": self._applicable_offers(),
        }

    @staticmethod
    def _select_item_upgrade(category: CategoryView, tier, slot: SlotView, is_3p: bool, to_kw: float):
        available = []
        for item in category.items:
            if tier not in item.tiers:
                continue
            ft = slot.filter_type
            if ft == "hybrid":
                if item.inverter_type != "hybrid":
                    continue
            elif ft == "ongrid":
                if item.inverter_type and item.inverter_type != "ongrid":
                    continue
            if item.phase:
                if is_3p:
                    if ft == "hybrid":
                        if not ("HYB" in item.phase and "3P" in item.phase):
                            continue
                    elif "3P" not in item.phase:
                        continue
                elif ft == "hybrid":
                    if not ("HYB" in item.phase and "1P" in item.phase):
                        continue
                elif "3P" in item.phase:
                    continue
            available.append(item)
        if not available:
            return None
        if category.is_inverter and any(i.kw for i in available):
            return min(available, key=lambda i: abs(float(i.kw or 0) - to_kw))
        return available[0]


def quote(data, *, today: date) -> dict:
    """The website quote for ``data`` (the legacy ``/bom/api/calculate/`` request body) on ``today``."""
    params = parse_request(data)
    snapshot = load_snapshot(params["sys_type"])
    return Calculator(snapshot, params, today).compute()
