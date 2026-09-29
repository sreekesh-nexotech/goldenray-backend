"""A small synthetic pack world for the API and service tests (the parity test uses the real Flarize data).

``world()`` creates: a panel (``pnl1``, 540 W) and an alternative panel (``pnl2``), an inverter (``inv1``, 3 kW), all
VALUE tier; LIST prices; an ACTIVE market-rate set with ``ongrid_value / 3`` = 229 000; the GST configuration; a
published PriceRelease; and an APPROVED config version whose configuration offers the on-grid VALUE 3 kW pack and a
future-ready 3 → 5 pair (without a market rate: excluded from releases).
"""

from __future__ import annotations

import copy
from decimal import Decimal

from catalog.models import BomRole, Category, ComponentTier
from catalog.tests.factories import CategoryFactory, ComponentFactory, inverter, panel
from core.services import stamp_create
from packs.models import ConfigStatus, ConfigVersion
from packs.services import mirror
from pricing.models import MarketRateSetStatus
from pricing.services import releases as price_releases
from pricing.tests.factories import MarketRateFactory, MarketRateSetFactory, PriceFactory, gst_config

CONFIG = {
    "bomTemplates": {
        "ongrid": {
            "sizes": {"3": "3 kW", "5": "5 kW"},
            "tiers": ["value"],
            "threePhase": [],
            "slots": [
                {"pos": 0, "category": "panel", "label": "Solar Panel", "variable": True, "gst": 5, "qty": {"3": 6, "5": 9}, "salesSwap": True, "alternatives": [], "defaults": {"value:3": "pnl1"}},
                {"pos": 1, "category": "inverter", "label": "Inverter", "variable": True, "gst": 5, "qty": {"3": 1, "5": 1}, "salesSwap": True, "alternatives": [], "defaults": {}},
                {"pos": 2, "category": "isolator", "label": "AC Isolator", "variable": True, "gst": 18, "qty": {"3": 1, "5": 1}, "salesSwap": False, "alternatives": [], "defaults": {}},
            ],
            "fixedItems": [{"id": "fi_mc4", "name": "MC4 Connector", "gst": 18, "price": 56, "qty": {"3": 3, "5": 4}}],
        }
    },
    "structureTemplates": {"flatRoof": {"label": "Flat Roof", "items": [{"name": "Square Tube", "type": "tube", "tubeSize": "1.5x1.5", "weightKg": 12.2, "qty": {"3": 3, "5": 4}}]}},
    "tubeWeights": {"1.5x1.5": 12.2},
    "costs": {
        "serviceRateYear": 2000,
        "serviceYears": 5,
        "transportRate": 40,
        "miscellaneous": 1000,
        "office": 5500,
        "gpRatePerKg": 85,
        "giRatePerKg": 98.3,
        "structureLabor": 3000,
        "structureRepairPct": 10,
    },
    "installationMatrix": {"3": {"flat": 15000, "sheet": 18000, "elevated": 22000}, "5": {"flat": 22000, "sheet": 26000, "elevated": 32000}},
    "transportConfig": {"baseDistanceKm": 100, "costPerKm": 35, "vehicles": [{"vehicleType": "ACE", "vehicleName": "Tata Ace", "ratePerKm": 35}]},
    "marketRates": {"ongrid_value": {"3": 229000}},
    "gst": {"regime": "SOLAR_70_30_COMPOSITE", "ratePct": 8.9},
    "pricing": {"mode": "PACK_MARKET_RATE", "marginPct": 20},
    "futureUpgrade": {"pairs": [{"systemType": "ongrid", "panelSize": "3", "systemSize": "5"}]},
}


def config() -> dict:
    return copy.deepcopy(CONFIG)


def _tier(component, tier="VALUE"):
    ComponentTier.objects.create(component=component, tier=tier)
    return component


def profile():
    from bom.models import PackageProfile

    row = PackageProfile(key="ongrid_value", label="On-Grid Value", structure_material="GI", inverter_type="ONGRID")
    stamp_create(row, None)
    row.save()
    return row


def catalog():
    profile()
    pnl1 = _tier(panel(sku="pnl1", name="Panel 540", spec={"wattage_w": 540}))
    pnl2 = _tier(panel(sku="pnl2", name="Panel 550", spec={"wattage_w": 550}))
    inv1 = _tier(inverter(sku="inv1", name="Inverter 3kW", spec={"kw": Decimal("3.000")}))
    isolators = Category.objects.filter(slug="isolator").first() or CategoryFactory(slug="isolator", name="Isolator", bom_role=BomRole.PROTECTION, sku_prefix="ISO")
    is1 = _tier(ComponentFactory(sku="is1", name="AC Isolator 1P", category=isolators, attributes={"phase": "1P"}))
    for component, amount in ((pnl1, "13585.00"), (pnl2, "14000.00"), (inv1, "18250.00"), (is1, "1450.00")):
        PriceFactory(component=component, amount=Decimal(amount))
    return {"pnl1": pnl1, "pnl2": pnl2, "inv1": inv1, "is1": is1}


def price_release(user=None, *, rate="229000.00"):
    rate_set = MarketRateSetFactory(status=MarketRateSetStatus.ACTIVE)
    MarketRateFactory(set=rate_set, customer_price_incl_gst=Decimal(rate))
    gst_config()
    return price_releases.publish(user=user)


def version(*, status=ConfigStatus.APPROVED, number=1, cfg=None, user=None, based_on=None) -> ConfigVersion:
    row = ConfigVersion(number=number, status=status, config=cfg if cfg is not None else config(), based_on=based_on)
    stamp_create(row, user)
    row.save()
    mirror.mirror(row, user=user)
    return row


def world(user=None) -> dict:
    components = catalog()
    release = price_release(user)
    approved = version(user=user)
    return {**components, "price_release": release, "version": approved}
