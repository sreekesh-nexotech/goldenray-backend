"""``reference_kseb_tariff`` (PLAN §2.8): KSEB domestic energy-charge slabs.

Legacy ``kseb_tariffs`` (``min_units``, ``max_units`` null = open-ended, ``rate``) maps to ``slab_from_units``,
``slab_to_units``, ``rate_per_unit``. ``phase`` null means "every phase"; ``effective_from`` null means "in force
since before the platform existed". A schedule is the set of live, active rows sharing ``(phase, effective_from)``;
the one in force on a day is the latest ``effective_from`` not after it (``reference.services.lookups``).
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import F, Q

from reference.models.base import ReferenceRow


class KsebTariff(ReferenceRow):
    class Phase(models.TextChoices):
        SINGLE = "1P", "Single phase"
        THREE = "3P", "Three phase"

    slab_from_units = models.PositiveIntegerField(help_text="First unit (kWh per month) of the slab.")
    slab_to_units = models.PositiveIntegerField(null=True, blank=True, help_text="Last unit of the slab; empty = no upper limit.")
    phase = models.CharField(max_length=4, choices=Phase.choices, null=True, blank=True, help_text="Empty = applies to every phase.")
    rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4)
    fixed_charge = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    effective_from = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "reference_kseb_tariff"
        ordering = ["phase", "effective_from", "slab_from_units", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["phase", "effective_from", "slab_from_units"],
                condition=Q(deleted_at__isnull=True),
                nulls_distinct=False,
                name="reference_kseb_tariff_slab_live_uniq",
            ),
            models.CheckConstraint(condition=Q(phase__isnull=True) | Q(phase__in=["1P", "3P"]), name="reference_kseb_tariff_phase_valid"),
            models.CheckConstraint(condition=Q(slab_to_units__isnull=True) | Q(slab_to_units__gte=F("slab_from_units")), name="reference_kseb_tariff_slab_ordered"),
            models.CheckConstraint(condition=Q(rate_per_unit__gte=0), name="reference_kseb_tariff_rate_non_negative"),
            models.CheckConstraint(condition=Q(fixed_charge__gte=0), name="reference_kseb_tariff_fixed_charge_non_negative"),
        ]

    def __str__(self) -> str:
        upper = self.slab_to_units if self.slab_to_units is not None else "∞"
        return f"{self.slab_from_units}-{upper} units: ₹{self.rate_per_unit}/unit"
