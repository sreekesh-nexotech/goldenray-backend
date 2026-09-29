"""``bom_package_profile`` — Flarize ``catalog.json`` ``packageProfiles`` (PLAN §7.4; no PLAN table, DV-74).

One profile per package key (``ongrid_base``, ``hybrid_value`` …): the structure material, inverter family and the
default battery of the package. ``engines.bom_builder`` reads it (``getProfileKey``) for the default battery quantity.
"""

from django.db import models
from django.db.models import Q

from bom.models.choices import LIVE, ProfileInverterType, ProfileStructureMaterial, in_choices
from core.models import BaseModel


class PackageProfile(BaseModel):
    key = models.CharField(max_length=32, help_text="Flarize packageKey (ongrid_base, hybrid_value, premium …).")
    label = models.CharField(max_length=100)
    structure_material = models.CharField(max_length=4, choices=ProfileStructureMaterial.choices)
    inverter_type = models.CharField(max_length=8, choices=ProfileInverterType.choices)
    battery_included = models.BooleanField(default=False)
    battery_brand = models.CharField(max_length=100, blank=True, default="")
    battery_model = models.CharField(max_length=100, blank=True, default="")
    battery_capacity_kwh = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    battery_quantity = models.PositiveSmallIntegerField(default=0)
    # PROTECT: the default battery is a catalog master.
    battery_component = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="bom_package_profiles")
    structure_labor_override = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    repair_margin_override = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True, help_text="Fraction (0.1 = 10 %).")
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "bom_package_profile"
        ordering = ["key", "id"]
        constraints = [
            models.UniqueConstraint(fields=["key"], condition=LIVE, name="bom_package_profile_key_uniq"),
            models.CheckConstraint(condition=Q(key__regex=r"^[a-z0-9][a-z0-9_]*$"), name="bom_package_profile_key_format"),
            models.CheckConstraint(condition=in_choices("structure_material", ProfileStructureMaterial), name="bom_package_profile_material_valid"),
            models.CheckConstraint(condition=in_choices("inverter_type", ProfileInverterType), name="bom_package_profile_inverter_valid"),
            models.CheckConstraint(condition=Q(battery_capacity_kwh__gte=0), name="bom_package_profile_capacity_not_negative"),
            models.CheckConstraint(condition=Q(battery_quantity__lte=2), name="bom_package_profile_battery_quantity_band"),
        ]

    def __str__(self) -> str:
        return self.key
