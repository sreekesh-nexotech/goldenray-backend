"""The website calculators' sizing and price lookup tables (DV-58, DV-75).

The legacy tables ``solar_installations`` and ``solar_installation_new`` are not installations: they are what the
legacy ``calculate-solar`` and ``calculate-solar-new``/``calculate-solar-advanced`` endpoints looked up — per system
size, and per bill range and property type — to price a system (cost, subsidy, EMI rate, inverter price) and to show
the roof area and installation time. They stay the calculators' price source until the calculators read published
pack releases (PLAN §1.2); every legacy column has a typed home here.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel


class PropertyType(models.TextChoices):
    """Legacy ``solar_installation_new.type``; the label is what the legacy responses printed (``"type"``)."""

    RESIDENTIAL = "RESIDENTIAL", "Residential"
    COMMERCIAL = "COMMERCIAL", "Commercial"


class CapacitySize(BaseModel):
    """``calculators_capacity_size`` ← legacy ``solar_installations`` (one row per system size, ``calculate-solar``)."""

    power_capacity_kw = models.DecimalField(max_digits=7, decimal_places=3, help_text="System size (kW); the basic calculator looks rows up by it.")
    installation_days = models.PositiveIntegerField(help_text="Installation time in days (legacy time_to_complete).")
    total_cost = models.DecimalField(max_digits=14, decimal_places=2)
    total_subsidy = models.DecimalField(max_digits=14, decimal_places=2)
    area_required_sqft = models.PositiveIntegerField(help_text="Roof area in square feet (legacy area_required).")
    is_active = models.BooleanField(default=True, help_text="Inactive rows are ignored by the calculators.")

    class Meta:
        db_table = "calculators_capacity_size"
        ordering = ["power_capacity_kw", "id"]
        constraints = [
            # The legacy lookup was ``.get(power_capacity=kW)``: two rows for one size made it fail (HTTP 500).
            models.UniqueConstraint(fields=["power_capacity_kw"], condition=Q(deleted_at__isnull=True), name="calculators_capacity_size_kw_live_uniq"),
            models.CheckConstraint(condition=Q(power_capacity_kw__gt=0), name="calculators_capacity_size_kw_positive"),
            models.CheckConstraint(condition=Q(total_cost__gte=0) & Q(total_subsidy__gte=0), name="calculators_capacity_size_money_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.power_capacity_kw} kW"


class BillRangeSize(BaseModel):
    """``calculators_bill_range_size`` ← legacy ``solar_installation_new`` (per bill range and property type).

    ``calculate-solar-new`` picks the row of the bill's band (≤ 6000, 6001–8000, … 30001–40000 → the upper end) and
    property type; ``calculate-solar-advanced`` the smallest residential ``bill_range`` at or above the computed bill.
    """

    bill_range = models.PositiveIntegerField(help_text="Upper end (₹) of the monthly bill band this row prices.")
    property_type = models.CharField(max_length=16, choices=PropertyType.choices)
    power_capacity_kw = models.DecimalField(max_digits=7, decimal_places=3)
    installation_days_range = models.CharField(max_length=255, help_text='Installation time shown to the visitor, e.g. "3-7" (days).')
    total_cost = models.DecimalField(max_digits=14, decimal_places=2)
    total_subsidy = models.DecimalField(max_digits=14, decimal_places=2)
    area_required_sqft = models.PositiveIntegerField()
    loan_available = models.CharField(max_length=255, blank=True, default="", help_text='Loan range as shown, e.g. "2,00,000-6,00,000" or "N/A"; its first number is the graph\'s loan.')
    per_kw_rate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    final_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="Price after subsidy; the EMI is computed on it.")
    interest_rate = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True, help_text="Annual EMI interest rate as a fraction (0.0650 = 6.5 %).")
    inverter_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="Hybrid inverter price added to a backup battery.")
    is_active = models.BooleanField(default=True, help_text="Inactive rows are ignored by the calculators.")

    class Meta:
        db_table = "calculators_bill_range_size"
        ordering = ["property_type", "bill_range", "id"]
        constraints = [
            # The legacy lookup was ``.get(bill_range=…, type__iexact=…)``: duplicates made it fail (HTTP 500).
            models.UniqueConstraint(fields=["bill_range", "property_type"], condition=Q(deleted_at__isnull=True), name="calculators_bill_range_size_live_uniq"),
            models.CheckConstraint(condition=Q(property_type__in=PropertyType.values), name="calculators_bill_range_size_type_valid"),
            models.CheckConstraint(condition=Q(bill_range__gt=0), name="calculators_bill_range_size_range_positive"),
            models.CheckConstraint(condition=Q(power_capacity_kw__gt=0), name="calculators_bill_range_size_kw_positive"),
            models.CheckConstraint(condition=Q(total_cost__gte=0) & Q(total_subsidy__gte=0), name="calculators_bill_range_size_money_non_negative"),
            models.CheckConstraint(
                condition=(Q(per_kw_rate__isnull=True) | Q(per_kw_rate__gte=0)) & (Q(final_cost__isnull=True) | Q(final_cost__gte=0)) & (Q(inverter_price__isnull=True) | Q(inverter_price__gte=0)),
                name="calculators_bill_range_size_prices_non_negative",
            ),
            models.CheckConstraint(condition=Q(interest_rate__isnull=True) | (Q(interest_rate__gte=0) & Q(interest_rate__lt=1)), name="calculators_bill_range_size_rate_fraction"),
        ]
        indexes = [models.Index(fields=["property_type", "bill_range"], name="calculators_brs_type_range")]

    def __str__(self) -> str:
        return f"{self.get_property_type_display()} ≤ ₹{self.bill_range}"
