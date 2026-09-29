from decimal import Decimal

import factory

from emi.models import Bank, EmiSettings, InterestRateRule, SubsidyRule, SystemSize


class BankFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Bank

    name = factory.Sequence(lambda n: f"Bank {n}")
    abbr = "BNK"
    slug = factory.Sequence(lambda n: f"bank-{n}")
    annual_rate = Decimal("0.0850")
    max_loan = Decimal("500000.00")
    features = factory.LazyFunction(lambda: ["Fast approval"])


class InterestRateRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = InterestRateRule

    label = factory.Sequence(lambda n: f"Rule {n}")
    annual_rate = Decimal("0.0800")
    min_annual_rate = Decimal("0.0800")
    is_locked = True
    priority = 20


class SubsidyRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SubsidyRule

    label = factory.Sequence(lambda n: f"Subsidy {n}")
    kw_from = Decimal("3.00")
    amount = Decimal("78000.00")
    priority = 10


class SystemSizeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SystemSize

    label = factory.Sequence(lambda n: f"{n + 3}kW")
    capacity_kw = factory.Sequence(lambda n: Decimal(n + 3))
    price_per_kw = Decimal("66000.00")
    price_min = Decimal("100000.00")
    price_max = Decimal("900000.00")
    monthly_bill_reference = Decimal("10000.00")
    sort_order = factory.Sequence(lambda n: n)


class EmiSettingsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EmiSettings

    down_payment_quick_adds = factory.LazyFunction(lambda: [Decimal("5000.00"), Decimal("10000.00")])
