from datetime import date
from decimal import Decimal

import factory

from catalog.tests.factories import ComponentFactory
from pricing.models import (
    CostConfig,
    InstallationMatrix,
    MarketRate,
    MarketRateSet,
    Offer,
    Price,
    PriceKind,
    PriceSource,
    RoofAddon,
    StatutoryFee,
    StatutoryFeeKind,
    SwapDelta,
    SystemType,
    Tier,
)


class PriceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Price

    component = factory.SubFactory(ComponentFactory)
    kind = PriceKind.LIST
    amount = Decimal("1000.00")
    effective_from = factory.LazyFunction(lambda: date(2026, 1, 1))
    source = PriceSource.MANUAL


class MarketRateSetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = MarketRateSet

    name = factory.Sequence(lambda n: f"Set {n}")


class MarketRateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = MarketRate

    set = factory.SubFactory(MarketRateSetFactory)
    system_type = SystemType.ONGRID
    tier = Tier.VALUE
    size_key = "3"
    size_kw = Decimal("3")
    customer_price_incl_gst = Decimal("229000.00")


class SwapDeltaFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SwapDelta

    set = factory.SubFactory(MarketRateSetFactory)
    system_type = SystemType.ONGRID
    tier = Tier.VALUE
    slot = "panel"
    from_component = factory.SubFactory(ComponentFactory)
    to_component = factory.SubFactory(ComponentFactory)
    delta_incl_gst = Decimal("-1808.00")


class RoofAddonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RoofAddon

    set = factory.SubFactory(MarketRateSetFactory)
    structure_type = "SHEET_ROOF"
    size_kw = Decimal("3")
    addon_incl_gst = Decimal("14747.00")


class CostConfigFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CostConfig

    key = "install_rate"
    value = 3000
    effective_from = factory.LazyFunction(lambda: date(2026, 1, 1))


class InstallationMatrixFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = InstallationMatrix

    size_kw = Decimal("3")
    installation_type = "FLAT"
    install_cost = Decimal("15000.00")


class StatutoryFeeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StatutoryFee

    kind = StatutoryFeeKind.KSEB_REGISTRATION
    label = "3 KW"
    capacity_kw_max = Decimal("3")
    amount = Decimal("5400.00")
    effective_from = factory.LazyFunction(lambda: date(2026, 1, 1))


class OfferFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Offer

    code = factory.Sequence(lambda n: f"OFFER-T{n}")
    name = factory.Sequence(lambda n: f"Offer {n}")
    type = "FLAT"
    value = Decimal("5000.00")


GST_VALUES = {"gst_goods_share": 0.7, "gst_services_share": 0.3, "gst_goods_rate": 0.05, "gst_services_rate": 0.18}


def gst_config() -> None:
    for key, value in GST_VALUES.items():
        CostConfigFactory(key=key, value=value)
