"""``catalog_category`` (PLAN §2.2): how a component is taxed, measured, described and treated by the BOM builder.

``attribute_schema`` is a JSON Schema (draft 2020-12, root ``type: object``) that every live component's
``attributes`` must satisfy. ``bom_role`` also decides which spec table a component of the category carries
(``catalog.services.specs``). ``sku_prefix`` names the SKUs the platform generates (``<PREFIX>-0001``); imported
Flarize/BOM ids (``p1``, ``en7``) are kept as they are.
"""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class BomRole(models.TextChoices):
    MAIN_PANEL = "MAIN_PANEL", "Main panel"
    MAIN_INVERTER = "MAIN_INVERTER", "Main inverter"
    BATTERY = "BATTERY", "Battery"
    PROTECTION = "PROTECTION", "Protection"
    CABLE = "CABLE", "Cable"
    EARTHING = "EARTHING", "Earthing"
    STRUCTURE = "STRUCTURE", "Structure"
    SERVICE = "SERVICE", "Service"
    MISC = "MISC", "Miscellaneous"


class Unit(models.TextChoices):
    NOS = "NOS", "Numbers"
    M = "M", "Metres"
    KG = "KG", "Kilograms"
    SET = "SET", "Set"


class Category(BaseModel):
    slug = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    bom_role = models.CharField(max_length=24, choices=BomRole.choices, default=BomRole.MISC)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=4, help_text="Default GST rate as a fraction (0.18 = 18 %).")
    hsn_code = models.CharField(max_length=12, blank=True, default="")
    unit = models.CharField(max_length=12, choices=Unit.choices, default=Unit.NOS)
    attribute_schema = models.JSONField(default=dict, blank=True, help_text="JSON Schema for component.attributes.")
    sku_prefix = models.CharField(max_length=8)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "catalog_category"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="catalog_category_slug_live_uniq"),
            models.UniqueConstraint(Lower("sku_prefix"), condition=LIVE, name="catalog_category_sku_prefix_live_uniq"),
            models.CheckConstraint(condition=Q(bom_role__in=BomRole.values), name="catalog_category_bom_role_valid"),
            models.CheckConstraint(condition=Q(unit__in=Unit.values), name="catalog_category_unit_valid"),
            models.CheckConstraint(condition=Q(gst_rate__gte=0) & Q(gst_rate__lte=1), name="catalog_category_gst_rate_fraction"),
            models.CheckConstraint(condition=Q(sku_prefix__regex=r"^[A-Z][A-Z0-9]{1,7}$"), name="catalog_category_sku_prefix_format"),
        ]

    def __str__(self) -> str:
        return self.name
