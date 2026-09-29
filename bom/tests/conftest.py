from datetime import date
from decimal import Decimal

import pytest

from bom.tests.factories import FixedItemFactory, SlotFactory, StructureItemFactory, StructureTemplateFactory, TemplateFactory
from catalog.models import BomRole, ComponentStatus, Unit
from catalog.tests.factories import CategoryFactory, ComponentFactory, ComponentTierFactory, InverterSpecFactory
from pricing.tests.factories import CostConfigFactory, MarketRateFactory, MarketRateSetFactory, OfferFactory, PriceFactory


@pytest.fixture
def bom_user(make_user):
    return make_user(grants={"bom": "*", "catalog": ["view"]})


@pytest.fixture
def client(auth_client, bom_user):
    return auth_client(bom_user)


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"bom": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    """Authenticated, but holds no bom grant."""
    return auth_client(make_user(grants={"catalog": ["view"]}))


# ── a small hand-built configuration (on-grid template, structures, rates, offers) ──

COSTS = {
    "install_rate": 3000,
    "service_rate_year": 2000,
    "service_years": 5,
    "transport_rate_per_km": 40,
    "transport_base_km": 100,
    "miscellaneous": 1000,
    "office_per_project": 5500,
    "gp_rate_per_kg": 85,
    "gi_rate_per_kg": 130,
    "structure_labor": 1500,
    "structure_repair_pct": 0.1,
}


def _component(category, name, price, tiers=("BASE", "VALUE", "PREMIUM"), **attributes):
    component = ComponentFactory(category=category, name=name, attributes=attributes, status=ComponentStatus.ACTIVE)
    for tier in tiers:
        ComponentTierFactory(component=component, tier=tier)
    PriceFactory(component=component, amount=Decimal(price))
    return component


@pytest.fixture
def world():
    for key, value in COSTS.items():
        CostConfigFactory(key=key, value=value)
    ongrid = TemplateFactory(system_type="ONGRID", sizes=[{"key": "3", "label": "3 kW"}, {"key": "5tp", "label": "5kW 3P"}], three_phase_sizes=["5tp"])
    panels = CategoryFactory(slug="panel", bom_role=BomRole.MAIN_PANEL, gst_rate=Decimal("0.05"))
    inverters = CategoryFactory(slug="inverter", bom_role=BomRole.MAIN_INVERTER, gst_rate=Decimal("0.05"))
    cables = CategoryFactory(slug="dc_cable", unit=Unit.M)
    _component(panels, "Panel 540", "13122.00")
    for kw, phase in (("3", "1P"), ("5", "3P")):
        inverter = _component(inverters, f"Inverter {kw}", "16600.00")
        InverterSpecFactory(component=inverter, kw=Decimal(kw), phase=phase, inverter_type="ONGRID")
    _component(cables, "Cable 4sqmm", "43.00", tiers=("BASE",))
    SlotFactory(template=ongrid, key="panel", category=panels, sort_order=0, gst_rate=Decimal("0.05"), qty_rule={"type": "size_table", "qty": {"3": 6, "5tp": 9}})
    SlotFactory(template=ongrid, key="inverter", category=inverters, sort_order=1, gst_rate=None, filter_type="ONGRID", qty_rule={"type": "size_table", "qty": {"3": 1, "5tp": 1}})
    SlotFactory(template=ongrid, key="dc_cable", category=cables, sort_order=2, qty_rule={"type": "size_table", "qty": {"3": 50, "5tp": 60}})
    FixedItemFactory(template=ongrid, name="MC4", unit_price=Decimal("56.00"), qty_rule={"type": "size_table", "bat_lookup": "always", "qty": {"3": 3, "5tp": 4}})
    FixedItemFactory(template=ongrid, name="3P only", unit_price=Decimal("950.00"), qty=Decimal("1"), qty_rule=None, condition={"phases": ["3P"]})
    FixedItemFactory(template=ongrid, name="Tube", unit_price=Decimal("1.00"), is_tube=True)
    flat = StructureTemplateFactory(slug="flat_roof")
    StructureItemFactory(template=flat, weight_kg=Decimal("15.3"), qty_rule={"type": "kw_interpolated", "points": {"3": 3, "5": 4}})
    elevated = StructureTemplateFactory(slug="elevated")
    StructureItemFactory(template=elevated, weight_kg=Decimal("15.3"), qty_rule={"type": "kw_interpolated", "points": {"3": 5, "5": 6}})
    StructureItemFactory(template=elevated, item_type="FIXED", weight_kg=None, unit_price=Decimal("80.00"), qty_rule={"type": "kw_interpolated", "points": {"3": 4}})
    rates = MarketRateSetFactory(status="ACTIVE")
    MarketRateFactory(set=rates, tier="VALUE", size_key="3", customer_price_incl_gst=Decimal("230000.00"))
    OfferFactory(
        code="FLAT5K", name="Flat 5k", type="FLAT", value=Decimal("5000.00"), status="ACTIVE", applies_to_system="ALL", applies_to_tier="ALL", starts_on=date(2026, 1, 1), ends_on=date(2026, 12, 31)
    )
    OfferFactory(code="PCT3", name="3 %", type="PERCENT", value=Decimal("3.00"), status="ACTIVE", applies_to_system="ONGRID", applies_to_tier="BASE")
    OfferFactory(code="OLD", name="Old", type="FLAT", value=Decimal("1.00"), status="ACTIVE", ends_on=date(2025, 1, 1))
    OfferFactory(code="SIZE5", name="Only 5tp", type="FLAT", value=Decimal("1.00"), status="ACTIVE", applies_to_size_key="5tp")
    OfferFactory(code="DRAFT", name="Draft", type="FLAT", value=Decimal("1.00"), status="DRAFT")
    return ongrid
