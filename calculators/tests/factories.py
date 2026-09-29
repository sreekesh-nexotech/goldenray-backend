from decimal import Decimal

import factory

from calculators.models import BillRangeSize, CapacitySize, PropertyType


class CapacitySizeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CapacitySize

    power_capacity_kw = factory.Sequence(lambda n: Decimal(n + 1))
    installation_days = 5
    total_cost = factory.LazyAttribute(lambda row: Decimal(row.power_capacity_kw) * Decimal("68000.00"))
    total_subsidy = Decimal("78000.00")
    area_required_sqft = factory.LazyAttribute(lambda row: int(Decimal(row.power_capacity_kw) * 80))


class BillRangeSizeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BillRangeSize

    bill_range = factory.Sequence(lambda n: 6000 + n * 2000)
    property_type = PropertyType.RESIDENTIAL
    power_capacity_kw = Decimal("3.000")
    installation_days_range = "3-7"
    total_cost = Decimal("230000.00")
    total_subsidy = Decimal("78000.00")
    area_required_sqft = 240
    loan_available = "2,00,000"
    per_kw_rate = Decimal("76667.00")
    final_cost = Decimal("152000.00")
    interest_rate = Decimal("0.0650")
    inverter_price = Decimal("55000.00")
