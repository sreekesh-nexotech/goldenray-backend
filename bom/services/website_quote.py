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
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q

from bom.models import FixedItem, Slot, StructureItemType, StructureTemplate, Template
from bom.services.common import percent_of
from bom.services.qty_rules import QtyContext, evaluate, matches, new_panels
from catalog.models import Category, Component, ComponentStatus
from catalog.models.category import BomRole, Unit
from core.errors import DomainError
from engines._jscompat import js_round

VALID_SYS_TYPES = ("ongrid", "hybrid", "upgrade")
VALID_TIERS = ("base", "value", "premium")
VALID_SUBSIDY = ("residential", "ghs", "none")
VALID_MARGIN = ("percent", "flat")
VALID_STRUCTURE = ("flatRoof", "elevated", "sheetRoof")
#: Module content. Decides which panels may be offered and whether PM Surya Ghar applies;
#: for hybrid it also selects the price list (Flarize quotes DCR and non-DCR apart).
#: version-2's tri-state panel gate. `module_dcr` stays accepted as the two-state spelling.
VALID_PANEL_TYPE_FILTER = ("all", "DCR", "NON_DCR")
VALID_MODULE_DCR = ("dcr", "ndcr")
#: The phase of the system AFTER an upgrade. Chosen, not derived from the target size: a
#: customer going to 5 kW may stay single-phase or move to three.
VALID_UPGRADE_PHASE = ("1P", "3P")
#: Premium replaces the string inverter with per-panel micro-inverters; the two brands
#: carry different accessory sets, so the choice is the caller's.
VALID_MICRO_BRAND = ("enphase", "hoymiles")
#: Categories an upgrade to three phase needs but the template has no slot for.
#: The request's roof type, as the installation matrix spells it.
STRUCTURE_TO_INSTALLATION = {"flatRoof": "FLAT", "sheetRoof": "SHEET", "elevated": "ELEVATED"}

PHASE_CHANGE_CATEGORIES = ("meter", "meter_box", "isolator")
#: Cable a site can need beyond the standard run, priced at the category's own GST rather
#: than the composite rate (App.jsx:1321-1322) — the add-on ladder is not the material one.
EXTRA_CABLE_CATEGORIES = ("dc_cable", "ac_cable", "armoured_cable")
#: Categories holding micro-inverter accessories. Needed in the snapshot even though no
#: template slot refers to them — a premium build adds them from the tier, not a slot.
MICRO_CATEGORIES = ("enphase", "hoymiles", "ug_cable")
#: The accessories a premium build adds, by SKU.
#:
#: The source finds each with ``items.find(i => i.name.includes('IQ8P'))``
#: (App.jsx:1214), which is brittle twice over: a renamed row drops the part from every
#: premium quote, and first-match-by-array-order silently decides between near-duplicates.
#: That second hazard is live — ``System Controller`` matches ``en7`` at ₹110,000 before
#: ``en_sysctrl`` at ₹25,000, an ₹85,000 difference settled by catalog ordering. Pinning
#: SKUs preserves the source's actual choice and makes it reviewable instead of accidental.
ENPHASE_SKUS = {
    "micro": "en1",  # IQ8P — one per panel
    "cable": "en2",  # Q Cable — one per panel
    "envoy": "en3",
    "relay": "en4",
    "terminal": "en5",
    "ct": "en6",
    "controller": "en7",
    "battery": "en_flexbat",
}
HOYMILES_SKUS = {"hms2000": "hm1", "hms1000": "hm2", "dtu_pro": "hm3", "dtu_lite": "hm4", "end_cap": "hm5", "trunk": "hm6"}
UG_CABLE_SKUS = {"1P": "ug_2core", "3P": "ug_4core"}
#: A hybrid premium build runs its own underground cable, at a fixed length.
UG_CABLE_METRES = 40
STRUCTURE_SLUGS = {"flatRoof": "flat_roof", "elevated": "elevated", "sheetRoof": "sheet_roof"}
DEFAULT_SECTIONS = {"panels": True, "structure": True, "inverter": True, "hybrid_inv": False, "battery": False, "wiring": True}
#: Which parts of a future-upgrade build are sized for the LARGER future system. Anything
#: switched off is sized for today's panels instead — never omitted (App.jsx:1070-1076).
DEFAULT_FUTURE_OPTS = {"structure": True, "db": True, "cables": True, "meter": True}
#: Categories each future-upgrade option downsizes to panel kW when it is switched off.
FUTURE_DOWNSIZE = {
    "db": ("dcdb", "acdb"),
    "cables": ("dc_cable", "ac_cable", "earth_cable", "la_cable", "cb_rod", "isolator"),
    "meter": ("meter",),
}
#: Consumables are classified by NAME on the future-upgrade path, because a legacy fixed
#: item carries no category of its own (App.jsx:1262-1283). Transcribed verbatim, in the
#: source's evaluation order. Note that earthing rides on the *cables* option, not a
#: separate one, and that there is no `db` pattern here — DCDB and ACDB are variable
#: slots, not fixed items.
FUTURE_FIXED_PATTERNS = {
    "structure": re.compile(
        r"clamp|j bolt|base plate|square plate|anchor|epoxy|primer|thinner|roller|silicon|cotton|cutting|welding|self screw|end cap|corroshield|panel.*earth.*nut",
        re.IGNORECASE,
    ),
    "cables": re.compile(
        r"pipe|bend|elbow|saddle|gland|cable tie|sleeve|plug$|lug|tape|insulation|mc4|cable tray"
        r"|earth|lightning|la .*holder|la base|cb rod|10 swg|terminal strip",
        re.IGNORECASE,
    ),
    "meter": re.compile(r"meter box", re.IGNORECASE),
}


def _future_downsizes_fixed_item(fi: FixedView, opts: dict) -> bool:
    """Whether this consumable is sized for today's panels rather than the future system."""
    return any(not opts.get(option, True) and pattern.search(fi.name or "") for option, pattern in FUTURE_FIXED_PATTERNS.items())
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
#: The four cost-config keys engines.money composes into the effective GST rate.
GST_SPLIT_KEYS = ("gst_goods_share", "gst_services_share", "gst_goods_rate", "gst_services_rate")
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
    if not _hashable_in(data.get("module_dcr", "dcr"), VALID_MODULE_DCR):
        messages.append(("module_dcr", "module_dcr must be dcr | ndcr"))
    if data.get("panel_type_filter") is not None and not _hashable_in(data.get("panel_type_filter"), VALID_PANEL_TYPE_FILTER):
        messages.append(("panel_type_filter", "panel_type_filter must be all | DCR | NON_DCR"))
    if not _hashable_in(data.get("upgrade_phase", "1P"), VALID_UPGRADE_PHASE):
        messages.append(("upgrade_phase", "upgrade_phase must be 1P | 3P"))
    if not _hashable_in(data.get("micro_brand") or "enphase", VALID_MICRO_BRAND):
        messages.append(("micro_brand", "micro_brand must be enphase | hoymiles"))
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
    if data["sys_type"] != "upgrade":
        # the standard calculator computes with the size's kW: NaN / ±∞ / huge made the legacy rounding raise (HTTP 500)
        kw = Calculator._size_to_kw(str(data["size"]))
        if not math.isfinite(kw) or abs(kw) > MAX_MAGNITUDE:
            problems.append(("size", f"size must be a size key or a finite number of kW of at most {MAX_MAGNITUDE:.0f}"))
    # any other value (an object, a list, a number) matches no offer id, as in the legacy view
    offer_id = data.get("selected_offer_id") or None
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
        "module_dcr": data.get("module_dcr", "dcr"),
        # Absent means the caller is using the two-state `module_dcr`; see
        # `_wanted_panel_type`.
        "panel_type_filter": data.get("panel_type_filter"),
        "upgrade_phase": data.get("upgrade_phase", "1P"),
        "ghs_houses": max(1, ghs_houses),
        "selected_offer_id": offer_id,
        "custom_discount": custom,
        "margin_type": data.get("margin_type", "percent"),
        "margin_val": margin_val,
        "upgrade_from_kw": from_kw,
        "upgrade_to_kw": to_kw,
        "upgrade_sections": sections,
        # Future-upgrade ("upgrade-ready"): panels go in at `size` today while the
        # inverter and chosen balance-of-system are sized for `future_size`, so the
        # customer can add panels later without replacing them.
        "future_size": str(data.get("future_size") or ""),
        "future_opts": {**DEFAULT_FUTURE_OPTS, **(data.get("future_opts") or {})},
        # Cable the site needed beyond the standard run, charged over the rate.
        "extra_cable": _extra_cable(data.get("extra_cable")),
        # Premium tier only: which micro-inverter brand, and whether the gateway has to
        # enforce zero export (a KSEB requirement on some connections).
        "micro_brand": data.get("micro_brand") or "enphase",
        "zero_export": bool(data.get("zero_export", True)),
    }


def _extra_cable(value) -> list[dict]:
    """``[{"category": "dc_cable", "qty": 40, "component": "<sku>"?}, …]``.

    A long run to the meter, or an armoured length under a driveway, is site-specific: it
    is not in any template, so it arrives per quote. Rows without a usable category and a
    positive quantity are dropped rather than rejected — an empty add-on is not an error,
    it is the normal case.
    """
    if not isinstance(value, list):
        return []
    rows = []
    for row in value:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "")
        if category not in EXTRA_CABLE_CATEGORIES:
            continue
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or qty > MAX_MAGNITUDE:
            continue
        rows.append({"category": category, "qty": qty, "component": str(row.get("component") or "")})
    return rows


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
    #: Panels only: ``catalog.PanelSpec.is_dcr``. ``None`` for anything that is not a
    #: panel, and for a panel whose content has never been recorded.
    dcr: bool | None = None
    #: So a quote can ask for one specific part -- the extra cable a site needed.
    sku: str = ""
    #: Engineering values the string/MPPT layout needs, in engines.string_config's field
    #: names. Empty for anything without a panel or inverter spec.
    specs: dict | None = None
    #: The item's own GST rate where it differs from its category's. The source reads
    #: `item.gstOverride || cat.gstDefault || 18` (App.jsx:1939), and four of the nine
    #: Enphase accessories carry one -- the Q Relay and the System Controller are 18%
    #: against the category's 5%. Taxing those at the category rate under-charges GST on
    #: every premium quote.
    gst_override: float | None = None


@dataclass
class CategoryView:
    slug: str
    gst_default: int | float
    unit_m: bool
    is_inverter: bool
    is_panel: bool = False  # only panels are gated on DCR content
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
    slot_category_gst: int | float | None  # the category's rate when a slot of the template uses it (legacy cat_map), else None
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
    name: str = ""  # quoted as a material line under COMPOSITE, so the name is part of the BOM


@dataclass
class Snapshot:
    costs: dict
    three_phase_sizes: list
    slots: list[SlotView]
    fixed_items: list[FixedView]
    structures: dict[str, list[StructureItemView]]
    market_rates: list[dict]
    offers: list[dict]
    #: ``{(size_key, installation_type): cost}``. version-2 prices installation from this
    #: matrix rather than a flat per-kW rate, so a 3 kW flat roof is ₹15,000 and not
    #: 3 × ₹3,000. Empty leaves the per-kW fallback in place, which is what the source
    #: does when the matrix has no cell (`instMatrixCost || bomKw * installRate`).
    installation_matrix: dict[tuple[str, str], float] = field(default_factory=dict)
    #: ``{profile_key: battery_quantity}``. version-2 derives the battery band from the
    #: package profile instead of taking it from the request, so the band is a property of
    #: the package being sold rather than something a caller can ask for.
    battery_bands: dict[str, int] = field(default_factory=dict)
    #: Categories reachable by slug rather than through a slot -- the parts an upgrade to
    #: three phase needs (:data:`PHASE_CHANGE_CATEGORIES`). Empty for the standard path.
    categories_by_slug: dict[str, CategoryView] = field(default_factory=dict)


def _costs() -> dict:
    from pricing.services.cost_config import current_values

    values = current_values()
    missing = [key for key in COST_KEYS.values() if key not in values]
    if missing:
        raise DomainError("quote_not_configured", f"The quote engine needs the cost configuration keys {', '.join(missing)}.", status=503, errors={"cost_config": missing})
    costs = {name: values[key] for name, key in COST_KEYS.items()}
    costs["structure_repair_pct"] = float(Decimal(str(costs["structure_repair_pct"])) * 100)
    costs["extra_km_rate"] = values.get("transport_extra_rate_per_km", LEGACY_EXTRA_KM_RATE)
    # Optional, like `extra_km_rate` above: version-2 amortises the office overhead across
    # the month's projects (App.jsx:2220-2222), but a deployment that has not configured
    # the monthly figures still prices -- `_office_cost` falls back to the flat per-project
    # key. Listing them in COST_KEYS would make them MANDATORY and 503 every quote, which
    # is exactly what it did: 32 tests failed on `quote_not_configured` because most
    # fixtures set only the flat key.
    costs["office_expense_monthly"] = values.get("office_expense_monthly")
    costs["expected_projects_per_month"] = values.get("expected_projects_per_month")
    costs["gst_regime"], costs["gst_rate_pct"] = _gst_regime(values)
    return costs


def _gst_regime(values: dict) -> tuple[str, float]:
    """``(mode, effective_rate_pct)`` for this quote, via ``engines.money``.

    ``COMPOSITE`` taxes the selling price once. The rate comes from
    ``gst.quote_regime.rate_pct`` when stated — Golden Ray invoices solar at a flat 9 %,
    which the 70/30 split at 5/18 only approximates (8.9 %), and a quote has to show the
    rate actually charged. Otherwise it is derived from the four ``gst_*`` split keys, the
    same scheme ``PriceRelease.payload["gst"]`` already stamps.

    Absent key means ``PER_LINE``, so the public website quote
    (``legacy/services/website.py``) is never re-priced by a deployment — only by an
    admin deliberately setting the regime.
    """
    regime_config = values.get("gst.quote_regime") or {}
    if regime_config.get("mode") != "COMPOSITE":
        return "PER_LINE", 0.0
    from engines.money import GstConfig, GstRegimeCode, resolve_gst_regime

    stated = regime_config.get("rate_pct")
    if stated is not None:
        regime = resolve_gst_regime(GstConfig(regime=GstRegimeCode.FLAT, rate_pct=Decimal(str(stated))))
        return "COMPOSITE", float(regime.effective_rate_pct)
    missing = [key for key in GST_SPLIT_KEYS if key not in values]
    if missing:
        raise DomainError(
            "quote_not_configured",
            f'COMPOSITE GST needs gst.quote_regime.rate_pct, or the split keys {", ".join(missing)}.',
            status=503,
            errors={"cost_config": missing},
        )
    regime = resolve_gst_regime(GstConfig.from_cost_config(**{key: values[key] for key in GST_SPLIT_KEYS}))
    return "COMPOSITE", float(regime.effective_rate_pct)


def _legacy_item(component: Component, price: Decimal) -> Item:
    spec = getattr(component, "inverter_spec", None)
    panel = getattr(component, "panel_spec", None)
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
        dcr=panel.is_dcr if panel is not None else None,
        sku=component.sku,
        specs=_specs(panel, spec),
        gst_override=float(component.gst_rate_override * 100) if component.gst_rate_override is not None else None,
    )


def _specs(panel, inverter) -> dict | None:
    """Panel or inverter engineering values for :mod:`engines.string_config`.

    Returned as plain floats in that engine's vocabulary, so the engine stays free of both
    Django and the catalog's field naming.
    """
    def number(value):
        return float(value) if value is not None else None

    if panel is not None:
        return {
            "voc": number(panel.voc_v),
            "vmp": number(panel.vmp_v),
            "isc": number(panel.isc_a),
            "temp_coeff_voc": number(panel.temperature_coefficient_voc),
            "watt": panel.wattage_w,
            "max_sys_voltage": panel.max_system_voltage_v,
        }
    if inverter is not None:
        return {
            "mppt_count": inverter.mppt_count,
            "mppt_voltage_min": inverter.mppt_voltage_min_v,
            "mppt_voltage_max": inverter.mppt_voltage_max_v,
            "max_input_voltage": inverter.max_pv_voltage_v,
            "max_input_current": number(inverter.max_isc_a),
            "max_strings_per_mppt": inverter.max_strings_per_mppt,
            "is_micro": inverter.topology == "MICRO",
            "optimizer_based": False,
        }
    return None


def _items(category_ids: set[int], extra_component_ids: set[int]) -> tuple[dict[int, list[Item]], dict[int, Item]]:
    from pricing.models import Price, PriceKind

    components = list(
        Component.objects.filter(Q(category_id__in=category_ids) | Q(pk__in=extra_component_ids), status__in=QUOTABLE)
        .select_related("inverter_spec", "panel_spec")
        .prefetch_related("tiers")
        # Catalog order, which is what the source means by `available[0]` -- the first
        # item of the category's array that survives the filters. Ordering by `created_at`
        # only coincided with it until version-2 gave one panel an authored date, at which
        # point that panel became the default on every base and value quote.
        .order_by("sort_order", "created_at", "id")
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
    # Moving an upgrade to three phase pulls in parts the template has no slot for -- a 3P
    # meter, its box and a 4-pole isolator (App.jsx:2078-2080). They have to be in the
    # snapshot or the branch has nothing to choose from.
    wanted = EXTRA_CABLE_CATEGORIES + MICRO_CATEGORIES + (PHASE_CHANGE_CATEGORIES if sys_type.lower() == "upgrade" else ())
    extra = list(Category.objects.filter(slug__in=wanted, deleted_at__isnull=True))
    category_ids = {slot.category_id for slot in slots} | {category.pk for category in extra}
    items_by_category, items_by_id = _items(category_ids, {row.component_id for row in fixed if row.component_id})
    categories: dict[int, CategoryView] = {}
    for category in [slot.category for slot in slots] + extra:
        if category.pk not in categories:
            categories[category.pk] = CategoryView(
                slug=category.slug,
                gst_default=percent_of(category.gst_rate),
                unit_m=category.unit == Unit.M,
                is_inverter=category.bom_role == BomRole.MAIN_INVERTER,
                is_panel=category.bom_role == BomRole.MAIN_PANEL,
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
                slot_category_gst=categories[category.pk].gst_default if category is not None and category.pk in categories else None,
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
        installation_matrix=_installation_matrix(),
        battery_bands=_battery_bands(),
        categories_by_slug={category.slug: categories[category.pk] for category in extra},
    )


def _plain(number: Decimal | None):
    if number is None:
        return 0
    return int(number) if number == number.to_integral_value() else float(number)


def _battery_bands() -> dict[str, int]:
    """Each package profile's default battery quantity, keyed by profile key.

    version-2 made the battery band authoritative in the profile (App.jsx:1674-1690,
    `resolveBattery`): the package decides how many batteries it carries, so a hybrid
    value package is a one-battery package by definition rather than because a caller
    said so.
    """
    from bom.models.profiles import PackageProfile

    return {row.key: int(row.battery_quantity) for row in PackageProfile.objects.filter(deleted_at__isnull=True)}


def _installation_matrix() -> dict[tuple[str, str], float]:
    """Installation cost by (size key, roof type), the way version-2 prices it."""
    from pricing.models import InstallationMatrix

    return {
        (row.size_key, row.installation_type): float(row.install_cost)
        for row in InstallationMatrix.objects.all()
    }


def _structures() -> dict[str, list[StructureItemView]]:
    result: dict[str, list[StructureItemView]] = {}
    for template in StructureTemplate.objects.prefetch_related("items"):
        items = sorted(template.items.all(), key=lambda item: (item.sort_order, item.pk))
        result[template.slug] = [
            StructureItemView(item_type=item.item_type, weight_kg=item.weight_kg, price=item.unit_price, rule=item.qty_rule, name=item.name)
            for item in items
        ]
    return result


def _market_rates(sys_type: str) -> list[dict]:
    from pricing.models import MarketRate, MarketRateSetStatus

    # `variant` rows are excluded: `diffBase` carries a baseline, not a price. Future-size
    # rows ARE loaded -- an upgrade-ready build is quoted off its own rate
    # (`ongrid_value_up5sp`), which is higher than the plain one because the inverter and
    # balance-of-system are built for the larger system.
    rows = MarketRate.objects.filter(set__status=MarketRateSetStatus.ACTIVE, set__deleted_at__isnull=True, system_type=sys_type.upper(), variant="").order_by("sort_order", "id")
    return [
        {
            "tier": row.tier.lower(),
            "bat_config": row.battery_config,
            # Hybrid is quoted separately for DCR and non-DCR modules; on-grid carries a
            # blank, which matches any request.
            "dcr": row.dcr,
            "future_size": row.future_size_key,
            "size_key": row.size_key,
            "from_size": row.from_size_key,
            "value": row.customer_price_incl_gst,
        }
        for row in rows.only("tier", "battery_config", "dcr", "future_size_key", "size_key", "from_size_key", "customer_price_incl_gst")
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

    @property
    def _composite(self) -> bool:
        """Whether this quote prices the way Golden-Ray-BOM does.

        Every behaviour the BOM port *adds* has to be gated on this, not just the GST
        arithmetic. ``PER_LINE`` is the legacy engine's behaviour and the public website's
        live pricing, so a new line item or a new charge over the market rate that fires
        under it silently re-prices real customer quotes. The parity pin in
        ``test_quote_parity.py`` catches it, and has twice.
        """
        return self.costs["gst_regime"] == "COMPOSITE"

    def _micro_lines(self, tier: str, panels: int, is_three_phase: bool, bat_config: str, next_pos: int) -> list:
        """Micro-inverter accessories a premium build needs (App.jsx:1211-1259).

        Premium swaps one string inverter for one micro-inverter per panel, which brings a
        cluster of parts no template slot describes: the micros, a cable per panel, a
        gateway, a relay, and on three phase the terminals and current transformers the
        extra phases need. Quantities are per-panel arithmetic, so they follow the panel
        count the engine actually derived from the chosen module's wattage rather than the
        nominal system size.
        """
        # COMPOSITE only. The legacy engine never added these at all -- its captured
        # premium bodies carry no enphase or hoymiles line -- so emitting them under
        # PER_LINE would re-price the public website's premium quotes by the cost of a
        # whole micro-inverter set. The parity pin caught exactly that.
        if not self._composite:
            return []
        if tier != "premium" or self.p["sys_type"] not in ("ongrid", "hybrid"):
            return []
        is_hybrid = self.p["sys_type"] == "hybrid"
        # Hybrid premium is always Enphase: the battery and system controller are Enphase
        # parts, so the brand choice does not apply there (App.jsx:1212).
        brand = "enphase" if is_hybrid else self.p.get("micro_brand", "enphase")
        lines: list = []

        def add(slug: str, sku: str, qty) -> None:
            if not qty:
                return
            category = self.s.categories_by_slug.get(slug)
            if category is None:
                return
            item = next((i for i in category.items if i.sku == sku), None)
            if item is None:
                return
            unit_price = float(item.price)
            amount = qty * unit_price
            # `item.gstOverride || cat.gstDefault || 18` (App.jsx:1939).
            gst_pct = item.gst_override if item.gst_override is not None else category.gst_default
            lines.append(
                {
                    "pos": next_pos + len(lines),
                    "name": item.name,
                    "brand": item.brand,
                    "qty": qty,
                    "unit": "m" if category.unit_m else "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": round(amount * gst_pct / 100),
                    "section": slug,
                    "is_variable": False,
                }
            )

        if brand == "enphase":
            add("enphase", ENPHASE_SKUS["micro"], panels)
            add("enphase", ENPHASE_SKUS["cable"], panels)
            add("enphase", ENPHASE_SKUS["envoy"], 1)
            add("enphase", ENPHASE_SKUS["relay"], 1)
            # Three phase needs a terminal per phase, and current transformers a
            # single-phase install does not use at all.
            add("enphase", ENPHASE_SKUS["terminal"], 3 if is_three_phase else 1)
            add("enphase", ENPHASE_SKUS["ct"], 4 if is_three_phase else 0)
            if is_hybrid:
                add("enphase", ENPHASE_SKUS["controller"], 1)
                add("enphase", ENPHASE_SKUS["battery"], int(bat_config) if str(bat_config).isdigit() else 0)
                # Four cores on three phase, two on single.
                add("ug_cable", UG_CABLE_SKUS["3P" if is_three_phase else "1P"], UG_CABLE_METRES)
        else:
            # Hoymiles packs four panels per HMS 2000; the remainder takes a single
            # HMS 1000 whatever its size. Two or three leftover panels are therefore
            # under-provisioned -- the source's commercial choice, not a slip
            # (App.jsx:1243-1245): a second 2000 for one spare panel costs more than the
            # output is worth.
            hms2000 = panels // 4
            hms1000 = 1 if panels - hms2000 * 4 >= 1 else 0
            add("hoymiles", HOYMILES_SKUS["hms2000"], hms2000)
            add("hoymiles", HOYMILES_SKUS["hms1000"], hms1000)
            # One gateway per 99 panels, and never none.
            add("hoymiles", HOYMILES_SKUS["dtu_pro" if self.p.get("zero_export", True) else "dtu_lite"], max(1, math.ceil(panels / 99)))
            add("hoymiles", HOYMILES_SKUS["end_cap"], 1)
            # One trunk cable per inverter, of either size.
            add("hoymiles", HOYMILES_SKUS["trunk"], hms2000 + hms1000)
        return lines

    def _extra_cable_lines(self, next_pos: int) -> list:
        """Cable the site needed beyond the standard run (App.jsx:1319-1344).

        Priced at the category's own GST rate, not the composite one. That reads like an
        inconsistency but is deliberate: these lines are charged over the market rate on
        the add-on ladder, and the composite rate applies to the material ladder that
        feeds `grand_total`. The source keeps the two apart, so the quote is internally
        consistent even though the same line appears in both.
        """
        if not self._composite:
            return []
        lines = []
        for row in self.p.get("extra_cable") or []:
            category = self.s.categories_by_slug.get(row["category"])
            if category is None or not category.items:
                continue
            item = next((i for i in category.items if i.sku == row["component"]), None) if row["component"] else None
            if item is None:
                if row["component"]:
                    # A named part that is not in the category is a mistake worth seeing,
                    # not something to silently substitute: the quote would carry a cable
                    # nobody chose.
                    continue
                # Nothing named: the category's first part, as the source's dropdown
                # defaults to (App.jsx:3309).
                item = category.items[0]
            qty = row["qty"]
            unit_price = float(item.price)
            amount = qty * unit_price
            gst_pct = category.gst_default
            lines.append(
                {
                    "pos": next_pos + len(lines),
                    "name": f"{item.name} (extra)",
                    "brand": item.brand,
                    "qty": qty,
                    "unit": "m" if category.unit_m else "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": gst_pct,
                    "gst_amt": round(amount * gst_pct / 100),
                    "section": category.slug,
                    "is_variable": False,
                    "is_add_on": True,
                }
            )
        return lines

    def _add_on_total(self, cost_bd: dict, lines: list) -> float:
        """What is charged over the price-list rate (App.jsx:1491-1496).

        Three summands, and they are deliberately different in kind:

        * the non-flat-roof structure *delta* plus its labour and repair allowance — a
          sheet roof costs more than the flat roof the rate assumes;
        * the beyond-base distance surcharge, which is the other half of why
          `cost_total` must not also contain it (see :meth:`_calc_costs`);
        * any extra cable the site needed, GST included, since the rate covers a standard
          run.
        """
        structure = cost_bd["structure_extra"] + cost_bd["structure_labor"] + cost_bd["structure_repair"]
        dist_km = self.p["dist_km"]
        transport_extra = max(0, dist_km - int(self.costs["default_dist_km"])) * self.costs["extra_km_rate"]
        extra_cable = sum(line["amount"] + line["gst_amt"] for line in lines if line.get("is_add_on"))
        return structure + transport_extra + extra_cable

    def _future_size(self) -> str:
        """The future system size, or ``""`` when this is not a future-upgrade build.

        Only honoured when it is genuinely larger than today's panels: a "future" size at
        or below the current one is not an upgrade path, and treating it as one would
        quote a smaller inverter than the panels need.
        """
        future = self.p.get("future_size") or ""
        if not future or future == self.p["size"]:
            return ""
        if self._size_to_kw(future) <= self._size_to_kw(self.p["size"]):
            return ""
        return future

    def _future_downsized_categories(self) -> frozenset[str]:
        """Categories left at today's panel size because their option is switched off.

        Switched off means "size for the panels going in now", never "leave it out" — the
        system still needs a DCDB (App.jsx:1070-1076).
        """
        if not self._future_size():
            return frozenset()
        opts = self.p.get("future_opts") or DEFAULT_FUTURE_OPTS
        return frozenset(slug for option, slugs in FUTURE_DOWNSIZE.items() if not opts.get(option, True) for slug in slugs)

    # ── standard BOM (ongrid / hybrid) ──

    def _compute_standard(self) -> dict:
        p = self.p
        size = p["size"]
        tier = p["tier"]
        bat_config = self._battery_band(p["sys_type"], tier)
        three_phase_sizes = self.s.three_phase_sizes or []
        # Future-upgrade: the panels go in at `size` while the inverter and the chosen
        # balance-of-system are built for `future_size`, so panels can be added later
        # without replacing them. An inverter of 6 kW or more needs a three-phase supply,
        # which drags the meter and ACDB with it (App.jsx:1058-1064).
        future = self._future_size()
        bom_size = future or size
        panel_is_three_phase = size in three_phase_sizes
        is_three_phase = (bool(future) and self._size_to_kw(future) >= 6) or bom_size in three_phase_sizes
        kw = self._size_to_kw(bom_size)
        composite = self._composite
        bom_lines = self._get_slot_lines(size, tier, bat_config, is_three_phase, panel_is_three_phase=panel_is_three_phase)
        # Accessory quantities are per-panel, so they take the count the engine actually
        # derived from the chosen module's wattage: a 3 kW system is 6 panels of 540 W but
        # 5 of 600 W, and one micro-inverter per panel makes that an ₹8,200 difference.
        panel_count = next((int(line["qty"]) for line in bom_lines if line["section"] == "panel"), 0)
        bom_lines += self._micro_lines(tier, panel_count, is_three_phase, bat_config, next_pos=len(bom_lines))
        bom_lines += self._get_fixed_lines(size, tier, bat_config, is_three_phase, panels=panel_count)
        # Under COMPOSITE the flat-roof structure is quoted as material, as the source
        # does; _calc_costs then leaves structure_base out of cost_total so it is not
        # charged twice.
        if composite:
            # Structure is sized separately: the frame carries whatever panels are
            # physically mounted, so with `structure` switched off it stays at today's
            # panel kW rather than the future system's (App.jsx:1361).
            struct_kw = self._size_to_kw(size) if (future and not (p.get("future_opts") or DEFAULT_FUTURE_OPTS).get("structure", True)) else kw
            bom_lines += self._get_structure_lines(struct_kw, next_pos=len(bom_lines))
        # Extra cable last, so it reads as an addition to the standard bill rather than
        # being lost among the slot lines.
        bom_lines += self._extra_cable_lines(next_pos=len(bom_lines))
        totals = self._calc_totals(bom_lines)
        # `size`, not `bom_size`: the source reads `instMatrix[size]` (App.jsx:2214), which
        # is today's system size even on an upgrade-ready build quoted at the future one.
        # So a 3 kW system wired for 5 kW is installed at the 3 kW price. That reads like
        # an oversight, but it is what the tool charges, and the installation really is
        # being done on today's array -- replicated and flagged either way.
        cost_bd = self._calc_costs(totals["material_with_gst"], kw, structure_as_material=composite, size_key=p["size"])
        if composite:
            # Source ladder (App.jsx:1477-1488): margin on PRE-GST cost, then one GST
            # charge on cost + profit. Taxing a GST-inclusive subtotal, as PER_LINE does,
            # would tax the tax.
            pre_gst = totals.pop("_material_ex_gst_exact") + cost_bd["cost_total"]
            margin = self._calc_margin(pre_gst)
            all_gst = round((pre_gst + margin) * self.costs["gst_rate_pct"] / 100)
            subtotal = pre_gst + margin
            grand_total = subtotal + all_gst
            totals = {**totals, "pre_gst_subtotal": round(pre_gst), "gst_on_sale": all_gst}
        else:
            totals.pop("_material_ex_gst_exact")
            subtotal = totals["material_with_gst"] + cost_bd["cost_total"]
            margin = self._calc_margin(subtotal)
            grand_total = subtotal + margin
            totals = {**totals, "pre_gst_subtotal": None, "gst_on_sale": None}
        market_rate = self._lookup_market_rate()
        # Site add-ons sit ON TOP of the price-list rate (App.jsx:1582): the rate is for a
        # standard flat-roof install at the included distance, so a sheet roof, a longer
        # run or extra cable is charged over it. When no rate is configured the quote falls
        # back to cost-plus, where those costs are already inside `grand_total`.
        #
        # COMPOSITE only. The legacy engine charged nothing over the rate -- its captured
        # bodies read `customer_price == market_rate` even at 175 km with an elevated
        # structure, because it folded the surcharge into `cost_total` instead. Applying
        # the add-on ladder under PER_LINE would re-price the public website, which is
        # exactly what the regime key exists to prevent; the parity pin caught it.
        add_on_total = self._add_on_total(cost_bd, bom_lines) if composite else 0
        customer_price = market_rate + add_on_total if market_rate > 0 else grand_total
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
            # The string/MPPT layout the installer needs. Not in INTERNAL_KEYS: it reveals
            # no cost, and an installer working from a public quote needs it. COMPOSITE
            # only, like every other behaviour the port adds (see `_composite`).
            **({"string_config": self._string_config(bom_lines, panel_count)} if composite else {}),
        }

    def _string_config(self, lines: list, panels: int) -> dict | None:
        """The layout for the panel and inverter this quote actually selected.

        Looked up from the chosen lines rather than recomputed, so the layout always
        describes the system being quoted — if the sales person swaps the inverter, the
        string check follows.
        """
        from engines.string_config import string_config

        panel_line = next((line for line in lines if line["section"] == "panel"), None)
        inverter_line = next((line for line in lines if line["section"] == "inverter"), None)
        if panel_line is None or inverter_line is None or not panels:
            return None
        panel = self._spec_for(panel_line, "panel")
        inverter = self._spec_for(inverter_line, "inverter")
        if panel is None or inverter is None:
            return None
        return string_config(panel=panel, inverter=inverter, panels=panels, is_three_phase=self.p.get("upgrade_phase") == "3P")

    def _spec_for(self, line: dict, slug: str) -> dict | None:
        """The engineering specs of the item on ``line``, in the engine's field names."""
        category = next((slot.category for slot in self.s.slots if slot.category.slug == slug), None)
        if category is None:
            return None
        item = next((i for i in category.items if i.name == line["name"]), None)
        if item is None:
            return None
        return item.specs or None

    def _context(self, size, tier, bat_config, is_three_phase) -> QtyContext:
        return QtyContext(size_key=size, tier=tier, bat_config=bat_config, kw=self._size_to_kw(size), is_three_phase=is_three_phase)

    def _get_slot_lines(self, size, tier, bat_config, is_three_phase, *, panel_is_three_phase: bool | None = None) -> list:
        """One line per filled slot.

        On a future-upgrade build each slot is sized for either today's panels or the
        larger future system, and takes the phase that goes with its own size — a DCDB
        left at panel kW must not be picked as three-phase just because the inverter is
        (App.jsx:1089, 1115-1116).
        """
        lines = []
        future = self._future_size()
        downsized = self._future_downsized_categories()
        if panel_is_three_phase is None:
            panel_is_three_phase = is_three_phase
        for slot in self.s.slots:
            cat = slot.category
            # Panels are always today's size; everything else follows the future size
            # unless its option is switched off.
            slot_size = size if (cat.is_panel or cat.slug in downsized or not future) else future
            slot_three_phase = panel_is_three_phase if (cat.slug in downsized and future) else is_three_phase
            qty = evaluate(slot.rule, self._context(slot_size, tier, bat_config, slot_three_phase))
            if not qty:
                continue
            item = self._select_item(cat, tier, slot, slot_three_phase, self._size_to_kw(slot_size))
            if not item:
                continue
            # Panel count comes from the module actually chosen, not the template's figure
            # (App.jsx:1169-1174). A 3 kW system is 6 panels of 540 W but 5 of 620 W, and
            # the template can only carry one of those. Getting this wrong is expensive
            # twice over: the panels themselves, and anything priced per panel -- a
            # premium build buys one micro-inverter per panel, so a spare panel is a spare
            # ₹8,200 inverter too.
            #
            # Gated like every other behaviour the port adds: deriving the count is the
            # better arithmetic, but it is still a re-pricing, and under PER_LINE this code
            # path serves the public website. The parity pin caught this one too -- a 3 kW
            # premium quote went from 6 panels to 5 and moved the customer's price.
            watt = (item.specs or {}).get("watt") if (cat.is_panel and self._composite) else None
            if watt:
                qty = math.ceil(self._size_to_kw(slot_size) * 1000 / float(watt))
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

    def _get_fixed_lines(self, size, tier, bat_config, is_three_phase, *, panels: int = 0) -> list:
        lines = []
        pos_offset = 500
        ctx = self._context(size, tier, bat_config, is_three_phase)
        # On a future-upgrade build, consumables belonging to a switched-off group are
        # sized for today's panels instead of the larger future system.
        future = self._future_size()
        panel_ctx = self._context(size, tier, bat_config, is_three_phase) if future else ctx
        future_ctx = self._context(future, tier, bat_config, is_three_phase) if future else ctx
        downsized = self.p.get("future_opts") or DEFAULT_FUTURE_OPTS
        for fi in self.s.fixed_items:
            if fi.is_tube:
                continue  # tube items are priced through the structure cost, not as BOM lines
            # The source classifies consumables by NAME, because a fixed item carries no
            # category of its own in the legacy data (App.jsx:1262-1283). Replicated
            # verbatim: a "cleaner" category-based rule would silently reclassify any item
            # whose name disagrees with its category.
            row_ctx = future_ctx if future else ctx
            if future and _future_downsizes_fixed_item(fi, downsized):
                row_ctx = panel_ctx
            if not matches(fi.condition, row_ctx):
                continue
            qty = evaluate(fi.rule, row_ctx)
            # MC4 connectors pair a panel to its micro-inverter, so a premium build needs
            # one per panel rather than the template's per-string figure (App.jsx:1296).
            if panels and tier == "premium" and self._composite and "mc4" in (fi.name or "").lower():
                qty = panels
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

    @classmethod
    def _upgrade_fixed_gst(cls, fi: FixedView):
        """Legacy upgrade rule ``fi.gst or (cat.gst_default if cat else 18)`` — ``cat`` only when a slot uses the
        category; a NULL rate (platform-only) is the category's rate as for standard lines."""
        if fi.gst is None:
            return cls._fixed_gst(fi)
        return fi.gst or (fi.slot_category_gst if fi.slot_category_gst is not None else 18)

    def _wanted_panel_type(self) -> bool | None:
        """Which module content may be offered: ``True`` DCR, ``False`` non-DCR, ``None`` either.

        version-2 turned the two-state ``moduleDcr`` into a tri-state filter and let a
        claimed subsidy override it (App.jsx:1781-1793)::

            if panelTypeFilter === 'DCR'      -> DCR only
            if panelTypeFilter === 'NON_DCR'  -> non-DCR only
            if subsidy is residential or ghs  -> DCR only, whatever the filter said
            otherwise                          -> either

        The subsidy branch is the one that matters commercially: PM Surya Ghar needs DCR
        modules, so a quote claiming it must not offer a panel that would forfeit it.

        ``module_dcr`` is still accepted as the deprecated two-state spelling, so the
        public website and existing integrations keep working.
        """
        explicit = self.p.get("panel_type_filter")
        if explicit == "DCR":
            return True
        if explicit == "NON_DCR":
            return False
        if explicit == "all":
            # Subsidy forces DCR even when the filter asks for anything.
            return True if self.p.get("subsidy_type") in ("residential", "ghs") else None
        # No tri-state field: the legacy two-state request.
        return self.p.get("module_dcr", "dcr") == "dcr"

    def _battery_band(self, sys_type: str, tier: str) -> str:
        """How many batteries this package carries: ``"0"``, ``"1"`` or ``"2"``.

        version-2 stopped taking the band from the request and derived it from the package
        profile (App.jsx:1674-1690). `resolveBattery` is 378 lines, but the quantity it
        returns is one line::

            const quantity = sysType === 'hybrid' ? effectiveDeclaredQty : 0;

        `effectiveDeclaredQty` is the profile's own `batteryQuantity` unless a Project Head
        override is supplied, and the source's comment records that the V1 UI never sets
        one -- so the profile is the answer. The rest of that module is identity,
        protection topology and validation, which the platform already models in
        `engines/battery_compat.py` and `BatterySpec`.

        Why this matters commercially: it is what stops a hybrid *value* quote shipping
        battery cabling and a change-over switch with no battery (the source's defect D1).
        A band that is a property of the package cannot disagree with the package.

        Non-hybrid is always ``"0"``: an on-grid system carries no battery whatever a
        caller asks for. Where no profile is seeded the request's `bat_config` still
        applies, so a deployment without `packageProfiles` keeps quoting rather than
        silently losing its batteries.

        COMPOSITE only, like every other behaviour this port adds. The profiles are
        seeded, and `hybrid_value` and `hybrid_premium` both declare a band of 1 -- so
        deriving under `PER_LINE` would hand a battery to a public-website caller that
        asked for `bat_config=0` and move the price it is quoted. "More correct" and "safe
        to change silently on a live website" are different claims; this is the fifth time
        that distinction has mattered.
        """
        requested = str(self.p.get("bat_config", "0"))
        if not self._composite:
            return requested
        if sys_type != "hybrid":
            return "0"
        band = self.s.battery_bands.get(f"hybrid_{tier}")
        return requested if band is None else str(band)

    def _select_item(self, category: CategoryView, tier, slot: SlotView, is_three_phase, kw_num):
        available = []
        # Module content gate (App.jsx:1132-1133). Only panels are filtered, and the
        # non-DCR side is `dcr is not True` rather than `is False`, so a panel whose
        # content was never recorded counts as non-DCR -- matching the source and erring
        # towards NOT promising a subsidy the panel may not qualify for.
        #
        # Tracked separately from `available` because the gate needs the source's safety
        # valve: when it empties the list, the line falls back to an ungated item instead
        # of vanishing. App.jsx:1164 explains why -- "the catalog has no Non-DCR premium
        # panel, for one" -- and a dropped panel line is far worse than an off-content
        # one, since the whole quote silently loses its largest cost.
        want_dcr = self._wanted_panel_type()
        off_content = []
        cross_tier = []
        for item in category.items:
            # Tracked separately for the same reason as the content gate below: when no
            # item in the requested tier survives the phase filter, the source falls back
            # across tiers rather than dropping the line — its `allAvailable`
            # (App.jsx:1137-1142) keeps the phase and content filters but not the tier.
            #
            # The isolator is the live example: the 1P part is base-only and the other is
            # 3P, so a premium single-phase build has no in-tier match and would otherwise
            # lose its isolator entirely.
            if not item.tiers:
                continue
            in_tier = tier in item.tiers
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
            if category.is_panel and want_dcr is not None and (item.dcr is True) != want_dcr:
                if in_tier:
                    off_content.append(item)
                continue
            if in_tier:
                available.append(item)
            else:
                cross_tier.append(item)
        # Fallbacks in the source's order: the requested tier first, then an off-content
        # panel, then any tier. Each step keeps the line rather than losing it.
        if not available:
            # Everything else passed but the content gate: quote the off-content panel
            # rather than dropping the line (App.jsx:1164-1167).
            #
            # The cross-tier step is COMPOSITE-only. The source does fall back across
            # tiers (its `allAvailable`, App.jsx:1137-1142), but under PER_LINE this path
            # serves the public website, where the legacy engine drops the line instead --
            # and resurrecting it adds a part, and its price, to 728 captured quotes. The
            # off-content fallback is not gated: it only ever *substitutes* a panel for a
            # panel, so it cannot add a line that was not already there.
            available = off_content or (cross_tier if self._composite else [])
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

    def _get_structure_lines(self, kw: float, *, next_pos: int) -> list:
        """The flat-roof structure as BOM material lines (App.jsx:1365-1385).

        A tube's unit price is its weight at the tier's rate, and its name carries that
        working so a quote shows why the number is what it is. GST is 18 % per line here
        even under COMPOSITE: the source writes it and it is what the add-on arithmetic
        reads, while :meth:`_calc_totals` ignores per-line GST in that regime anyway.
        """
        items = self.s.structures.get(STRUCTURE_SLUGS["flatRoof"]) or []
        tube_rate = self._cost("gp_rate_per_kg") if self.p["tier"] == "base" else self._cost("gi_rate_per_kg")
        ctx = QtyContext(kw=kw)
        lines = []
        for item in items:
            qty = evaluate(item.rule, ctx)
            if not qty or qty <= 0:
                continue
            if item.item_type == StructureItemType.TUBE:
                weight_kg = float(item.weight_kg or 0)
                # js_round, not round: JavaScript's Math.round breaks ties upward while
                # Python's rounds to even, and this product lands exactly on .5 for the
                # 2.5x1.5 tube at the base-tier GP rate (15.3kg x 85 = 1300.5) in all
                # three structure templates. Python gave 1300 against the source's 1301,
                # which is 3 rupees a quote through qty=3 and then compounds through the
                # margin. The other 42 round() calls in this module stay as they are:
                # they reproduce the legacy Python calculator, whose own banker's
                # rounding the retired golden file encodes.
                unit_price = js_round(weight_kg * tube_rate)
                name = f"{item.name} ({_plain(item.weight_kg)}kg × ₹{js_round(tube_rate)}/kg)"
            else:
                unit_price = float(item.price or 0)
                name = item.name
            amount = js_round(unit_price * qty)
            lines.append(
                {
                    "pos": next_pos + len(lines),
                    "name": name,
                    "brand": "",
                    "qty": qty,
                    "unit": "nos",
                    "unit_price": unit_price,
                    "amount": amount,
                    "gst_pct": 18,
                    "gst_amt": round(amount * 18 / 100),
                    "section": "structure",
                    "is_variable": False,
                }
            )
        return lines

    def _install_cost(self, kw: float, size_key: str, struct_type: str) -> float:
        """Installation from the matrix, falling back to the per-kW rate.

        version-2 prices installation per (size, roof) rather than per kW
        (App.jsx:2214-2215): a 3 kW flat roof is ₹15,000, where ``3 × install_rate`` gives
        ₹9,000. The fallback is the source's own ``instMatrixCost || bomKw * installRate``,
        so a size with no cell still prices rather than coming out free.
        """
        cell = self.s.installation_matrix.get((size_key, STRUCTURE_TO_INSTALLATION.get(struct_type, "FLAT")))
        return cell if cell else self._cost("install_rate") * kw

    @staticmethod
    def _size_key_for_kw(kw: float) -> str:
        """The matrix key for a plain kW figure (``3.0`` -> ``"3"``).

        The installation matrix is keyed the way the price list is -- ``3``, ``6``,
        ``5sp``/``5tp`` -- so a float has to be rendered without its decimal. 5 kW is
        phase-split and therefore has no bare ``"5"`` row; that lookup misses and the
        per-kW fallback applies, which is what the source does too.
        """
        return str(int(kw)) if float(kw).is_integer() else str(kw)

    def _office_cost(self) -> float:
        """Office overhead amortised across the month's projects.

        version-2 replaced the flat per-project figure with
        ``monthlyExpense / expectedProjectsPerMonth`` (App.jsx:2220-2222) -- ₹15,000 rather
        than ₹5,500 at the configured 150,000/10. Falls back to the flat key when the
        monthly figures are not configured, as the source does.
        """
        monthly = self.costs.get("office_expense_monthly")
        per_month = self.costs.get("expected_projects_per_month")
        if monthly and per_month:
            return round(float(monthly) / float(per_month))
        return self._cost("office")

    def _calc_costs(self, mat_with_gst: float, kw: float, *, structure_as_material: bool = False, size_key: str = "") -> dict:
        p = self.p
        dist_km = p["dist_km"]
        struct_type = p["structure_type"]
        composite = self._composite
        install = self._install_cost(kw, size_key, struct_type) if composite else self._cost("install_rate") * kw
        service = self._cost("service_rate_year") * self._cost("service_years")
        extra_km = max(0, dist_km - int(self.costs["default_dist_km"]))
        transport_extra = extra_km * self.costs["extra_km_rate"]
        if composite:
            # version-2 charges transport only BEYOND the included distance
            # (App.jsx:2218) -- at the base 100 km the cost is 0, where the legacy rate
            # billed the whole distance and made a local job carry ₹4,000.
            transport = transport_extra
        else:
            # The legacy engine keeps these on separate ladders: `costCalc.transportation`
            # is the whole distance at the per-km rate and feeds the internal cost, while
            # the beyond-base surcharge lives only in `addOnTotal`, which sits on top of
            # the market rate (App.jsx:1458, 1494). Folding the surcharge into cost_total
            # would charge the same distance twice on the one number the customer pays.
            transport = dist_km * self._cost("transport_rate") + transport_extra
        misc = self._cost("miscellaneous")
        office = self._office_cost() if composite else self._cost("office")
        structure_base = self._calc_structure_cost("flatRoof", kw)["total"]
        structure_extra = 0
        structure_labor = 0
        structure_repair = 0
        if struct_type != "flatRoof":
            selected = self._calc_structure_cost(struct_type, kw)
            structure_extra = max(0, selected["total"] - structure_base)
            structure_labor = self._cost("structure_labor")
            structure_repair = round((structure_extra + structure_labor) * float(self.costs["structure_repair_pct"]) / 100)
        # The flat roof is either an overhead or a material line, never both. When it has
        # been quoted as material it is still REPORTED here (a reader wants to see what
        # the structure came to) but must not be added into cost_total again. Likewise
        # the extra-km surcharge is reported inside `transport` but belongs to the add-on
        # ladder, so COMPOSITE leaves it out of the cost.
        cost_total = install + service + transport + misc + office + structure_extra + structure_labor + structure_repair
        if structure_as_material:
            cost_total -= transport_extra
        else:
            cost_total += structure_base
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

    def _calc_totals(self, lines: list) -> dict:
        """Material totals under the configured GST regime.

        ``PER_LINE`` sums each line's own GST into the 5/12/18 buckets. ``COMPOSITE``
        taxes the material once at the effective rate and reports the buckets as zero —
        Golden-Ray-BOM keeps the three fields "for backward compat but zero[s] them", and
        they stay in the response because the frontend's ``BomTotals``, the quotations
        snapshots and ``packs`` all read them.

        Under COMPOSITE the buckets are therefore written only by custom items, exactly
        as in the source.
        """
        mat_ex_gst = sum(line["amount"] for line in lines)
        if self.costs["gst_regime"] == "COMPOSITE":
            gst_total = round(mat_ex_gst * self.costs["gst_rate_pct"] / 100)
            gst_5 = gst_12 = gst_18 = 0
        else:
            gst_5 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 5)
            gst_12 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 12)
            gst_18 = sum(line["gst_amt"] for line in lines if line["gst_pct"] == 18)
            gst_total = gst_5 + gst_12 + gst_18
        return {
            "material_ex_gst": round(mat_ex_gst),
            "gst_5": round(gst_5),
            "gst_12": round(gst_12),
            "gst_18": round(gst_18),
            "gst_total": round(gst_total),
            "material_with_gst": round(mat_ex_gst + gst_total),
            # The ladder downstream must compound off the UNROUNDED material, as the
            # source does (App.jsx:1398-1403 sums line amounts and never rounds before
            # preGstSub). Rounding here first would shift margin and grand_total by the
            # fraction dropped -- 0.3 on a 3 kW on-grid base. Reported separately rather
            # than un-rounding material_ex_gst, whose integer shape the response contract
            # and BomQuoteTotalsSerializer both declare.
            "_material_ex_gst_exact": mat_ex_gst,
        }

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
        # An upgrade-ready build has its own rate, keyed on the future size as well as
        # today's -- `ongrid_value_up5sp` is not `ongrid_value`.
        future = self._future_size()
        for row in self.s.market_rates:
            if row["tier"] != tier or row["size_key"] != size:
                continue
            if row.get("future_size", "") != future:
                continue
            if sys_type == "ongrid" and not row["bat_config"] and not row["from_size"]:
                return self._rate(row["value"])
            if sys_type == "hybrid" and row["bat_config"] == bat_config:
                # Hybrid is priced per module content -- the two differ by ₹38k-₹64k, so a
                # DCR request must not be answered with a non-DCR rate. A blank on the row
                # means the price list does not distinguish, and matches either way.
                if row["dcr"] and row["dcr"] != p.get("module_dcr", "dcr"):
                    continue
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

    def _subsidy_eligible(self) -> bool:
        """PM Surya Ghar requires DCR modules (App.jsx:2411-2415).

        Checked AFTER the slab arithmetic, as the source does: the amount is computed and
        then forfeited, so a non-DCR quote reports 0 with the reason rather than silently
        skipping the scheme.
        """
        return self.p.get("module_dcr", "dcr") == "dcr"

    def _calc_subsidy(self, kw: float) -> float:
        p = self.p
        kind = p["subsidy_type"]
        if kind == "none":
            return 0
        if not self._subsidy_eligible():
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
        if kind == "none":
            return ""
        if not self._subsidy_eligible():
            return "Not eligible — Non-DCR modules"
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
        # The finished system's phase is the sales person's choice, not a function of its
        # size. The one override: hybrid inverters below 5 kW exist only as single-phase
        # (3P starts at 8 kW), so a 3P pick there is forced back (App.jsx:1837).
        is_3p = p.get("upgrade_phase", "1P") == "3P" and not (is_hyb_upgrade and to_kw <= 5)
        size_key = str(int(to_kw)) if to_kw == int(to_kw) else str(to_kw)
        ctx = QtyContext(size_key=size_key, tier=tier, bat_config="0", kw=to_kw, is_three_phase=is_3p, panels=panels, upgrade=True, upgrade_from_kw=from_kw, upgrade_to_kw=to_kw)
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
            # A three-phase upgrade runs more AC cable than a single-phase one -- four
            # conductors over the same route -- so the template's figure is replaced
            # rather than scaled (App.jsx:1910-1916). 1P keeps the template's 20 m, and
            # a hybrid keeps its own 30 m.
            if cat_slug == "ac_cable" and is_3p and not is_hyb_upgrade:
                qty = 40
            if not qty:
                continue
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
        if is_3p:
            pos = self._add_phase_change_lines(lines, tier, pos)
        for fi in self.s.fixed_items:
            # `is_tube` is NOT a skip here. The upgrade template's two tube rows are
            # ordinary fixed items priced from the catalog at a flat rate (₹450 / ₹600),
            # not by weight through the structure cost -- and the source never tests the
            # flag when iterating fixed items on either path (App.jsx:2115). Skipping
            # them dropped ₹2,289 of structure from every upgrade quote.
            #
            # The standard path keeps its skip: there, tubes genuinely come through
            # `_calc_structure_cost`, and quoting them as fixed items too would double
            # them. Only the upgrade template carries isTube fixed items, so the two
            # paths cannot collide.
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
            gst_pct = self._upgrade_fixed_gst(fi)
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
        # Installation is charged on the ADDED capacity, and the upgrade path carries no
        # service line and no extra-km transport (App.jsx:2195-2200).
        composite = self._composite
        if composite:
            # The same three cost inputs as the standard path (App.jsx:2969-2983), with
            # one upstream quirk kept: the matrix is read at the EXISTING system's size,
            # not at the upgrade target or the added capacity, so a 3→5 kW upgrade is
            # costed as a 3 kW install. That is what the source does (`upInstMatrix[size]`,
            # App.jsx:2970, where `size` is the base system's own state); replicated and
            # flagged rather than corrected, since diverging would put the platform at odds
            # with the tool on every upgrade quote.
            #
            # `from_kw`, not `p["size"]`: on the upgrade path the request's `size` is the
            # TARGET, so reading it would look up "5" — a key the matrix does not hold
            # (it has 5sp/5tp) — and fall through to the per-kW rate at ₹6,000.
            install = self._install_cost(to_kw - from_kw, self._size_key_for_kw(from_kw), p["structure_type"])
            extra_km = max(0, p["dist_km"] - int(self.costs["default_dist_km"]))
            transport = extra_km * self.costs["extra_km_rate"]
            office = self._office_cost()
        else:
            install = self._cost("install_rate") * (to_kw - from_kw)
            transport = p["dist_km"] * self._cost("transport_rate")
            office = self._cost("office")
        misc = self._cost("miscellaneous")
        cost_total = round(install + transport + misc + office)
        if self._composite:
            pre_gst = totals.pop("_material_ex_gst_exact") + cost_total
            margin = self._calc_margin(pre_gst)
            all_gst = round((pre_gst + margin) * self.costs["gst_rate_pct"] / 100)
            subtotal = pre_gst + margin
            grand_total = subtotal + all_gst
            totals = {**totals, "pre_gst_subtotal": round(pre_gst), "gst_on_sale": all_gst}
        else:
            totals.pop("_material_ex_gst_exact")
            subtotal = totals["material_with_gst"] + cost_total
            margin = self._calc_margin(subtotal)
            grand_total = subtotal + margin
            totals = {**totals, "pre_gst_subtotal": None, "gst_on_sale": None}
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
            # The phase actually used, not one inferred from the size -- what the quote
            # reports has to be what it priced.
            "meta": {"system_type": "upgrade", "size": f"{from_kw}→{to_kw}", "tier": tier, "bat_config": "0", "is_three_phase": is_3p, "new_panels": panels},
            "available_offers": self._applicable_offers(),
        }

    def _add_phase_change_lines(self, lines: list, tier: str, pos: int) -> int:
        """Parts a single-to-three-phase upgrade needs beyond the template (App.jsx:2051-2080).

        Moving the connection to three phase means a new 3P meter, a box for it and a
        4-pole isolator. The template has no slots for them because they are not part of
        the system being extended — they are the cost of changing the supply — so they are
        added here, in the inverter section, at 18 % GST.
        """
        # (slug, how to pick within the category). The source filters the meter to 3P
        # exactly, takes any meter box, and allows an isolator that is 3P or unmarked.
        wanted = (
            ("meter", lambda item: item.phase == "3P"),
            ("meter_box", lambda item: True),
            ("isolator", lambda item: item.phase == "3P" or not item.phase),
        )
        for slug, matches_phase in wanted:
            category = self.s.categories_by_slug.get(slug)
            if category is None:
                continue
            available = [item for item in category.items if tier in item.tiers and matches_phase(item)]
            if not available:
                continue
            item = available[0]
            unit_price = float(item.price)
            lines.append(
                {
                    "pos": pos,
                    "name": item.name,
                    "brand": item.brand,
                    "qty": 1,
                    "unit": "nos",
                    "unit_price": unit_price,
                    "amount": unit_price,
                    "gst_pct": 18,
                    "gst_amt": round(unit_price * 18 / 100),
                    "section": "inverter",
                    "is_variable": True,
                }
            )
            pos += 1
        return pos

    @staticmethod
    def _select_item_upgrade(category: CategoryView, tier, slot: SlotView, is_3p: bool, to_kw: float):
        """Pick the part for an upgrade slot (App.jsx:1925-1989).

        ``is_3p`` is the phase of the finished system, which the sales person chooses — it
        is NOT derived from the target size, since a customer moving to 5 kW may stay
        single-phase. The one exception is the DCDB: an upgrade always fits the 2/2 unit
        regardless of phase, because the extra strings need the inputs ("Normal upgrade
        DCDB: always 2/2", App.jsx:1948).
        """
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
            if category.slug == "dcdb":
                # Hybrid DCDB follows the phase; a plain upgrade DCDB is always the
                # 3P-tagged 2/2 part and never a hybrid one.
                if slot.filter_phase == "HYB":
                    if not item.phase or "HYB" not in item.phase:
                        continue
                    if ("3P" in item.phase) != is_3p:
                        continue
                else:
                    if item.phase and "HYB" in item.phase:
                        continue
                    if not item.phase or "3P" not in item.phase:
                        continue
            elif item.phase:
                if is_3p:
                    if ft == "hybrid":
                        if not ("HYB" in item.phase and "3P" in item.phase):
                            continue
                    elif "3P" not in item.phase or "HYB" in item.phase:
                        continue
                elif ft == "hybrid":
                    if not ("HYB" in item.phase and "1P" in item.phase):
                        continue
                elif "3P" in item.phase or "HYB" in item.phase:
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


# Business default B-1: the internal cost and margin never reach a public caller. ``quote()`` keeps the full legacy
# body (byte parity); every PUBLIC response — ``POST /api/public/v1/bom/quote/`` and the ``/legacy/bom/api/calculate/``
# shim — is filtered through :func:`public_body`. The website reads only ``bom_lines[].name/qty/unit`` and ``pricing.*``.
INTERNAL_KEYS = ("cost_breakdown", "totals")


def public_body(result: dict) -> dict:
    """The quote without ``cost_breakdown`` and ``totals``; every other key, in order, unchanged."""
    return {key: value for key, value in result.items() if key not in INTERNAL_KEYS}


#: Per-line money keys. Stripped for the warehouse's picking list, which is a document
#: about *what to load*, handled by people who have no reason to see what the customer paid.
DISPATCH_LINE_KEYS = ("unit_price", "amount", "gst_pct", "gst_amt")


def dispatch_body(result: dict) -> dict:
    """The picking list: what to load, with every price removed.

    Stripping happens here rather than in the browser. Hiding the columns client-side
    would still send every price to a machine on the warehouse floor, where a glance at
    the network tab -- or a screenshot of a mis-rendered page -- is a margin leak. The
    quote and the picking list are different documents, so they are different responses.

    ``pricing`` and ``available_offers`` go entirely; ``meta`` and ``string_config`` stay,
    because the installer reads the system size and the string layout off this sheet.
    """
    body = {
        key: value
        for key, value in public_body(result).items()
        if key not in ("pricing", "available_offers")
    }
    body["bom_lines"] = [
        {key: value for key, value in line.items() if key not in DISPATCH_LINE_KEYS}
        for line in body.get("bom_lines") or []
    ]
    return body
