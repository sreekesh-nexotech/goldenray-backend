"""``catalog_component`` — the single product record — and its tiers and change log (PLAN §2.2).

Columns beyond the PLAN list (every legacy/Flarize field has a home; mapping in docs/decisions/catalog.md):
``brand_label`` (the brand exactly as printed/sourced — legacy responses and BOM lines spell brands differently,
e.g. ``DEYE``/``Deye``), ``is_premium`` (Flarize ``isPremium``), ``warranty_extendable_years`` (website inverters),
``warranty_text`` (Flarize free text), ``engineering_status`` / ``notes`` (Flarize item notes), ``datasheet_url``
(battery master), ``retired_reason`` and ``status_changed_at``. ``brand`` is nullable: many BOM consumables
(MC4 connectors, screws, tape) have no brand and none is invented.
"""

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from catalog.models.category import Unit
from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class ComponentStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    DEPRECATED = "DEPRECATED", "Deprecated"
    RETIRED = "RETIRED", "Retired"


class Tier(models.TextChoices):
    BASE = "BASE", "Base"
    VALUE = "VALUE", "Value"
    PREMIUM = "PREMIUM", "Premium"


SEARCH_VECTOR = (
    SearchVector("sku", weight="A", config="simple")
    + SearchVector("name", weight="A", config="simple")
    + SearchVector("model", weight="A", config="simple")
    + SearchVector("brand_label", weight="B", config="simple")
    + SearchVector("description", weight="C", config="simple")
)


class Component(BaseModel):
    sku = models.CharField(max_length=32)
    # PROTECT: a category with components cannot disappear.
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="components")
    # PROTECT: a brand with components cannot disappear. Nullable: unbranded consumables.
    brand = models.ForeignKey("catalog.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="components")
    brand_label = models.CharField(max_length=100, blank=True, default="", help_text="Brand as printed (source spelling); defaults to the brand's name.")
    name = models.CharField(max_length=255)
    model = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")
    attributes = models.JSONField(default=dict, blank=True, help_text="Validated against the category's attribute_schema.")
    gst_rate_override = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    hsn_code_override = models.CharField(max_length=12, blank=True, default="")
    unit_override = models.CharField(max_length=12, blank=True, default="", choices=Unit.choices)
    status = models.CharField(max_length=12, choices=ComponentStatus.choices, default=ComponentStatus.DRAFT)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    deprecated_reason = models.TextField(blank=True, default="")
    retired_reason = models.TextField(blank=True, default="")
    # SET_NULL: the replacement suggestion is advisory; losing it never deletes this row.
    replacement = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces")
    is_public = models.BooleanField(default=False, help_text="Visible on the website (with a published public profile).")
    is_premium = models.BooleanField(default=False)
    warranty_product_years = models.PositiveSmallIntegerField(null=True, blank=True)
    warranty_performance_years = models.PositiveSmallIntegerField(null=True, blank=True)
    warranty_extendable_years = models.PositiveSmallIntegerField(null=True, blank=True)
    warranty_text = models.CharField(max_length=64, blank=True, default="")
    engineering_status = models.CharField(max_length=48, blank=True, default="", help_text="Source engineering note code (e.g. APPROVED_V1_1P_AC_ISOLATOR).")
    notes = models.TextField(blank=True, default="")
    # SET_NULL: optional documents; media.usage refuses deleting an asset while a live component references it.
    datasheet = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    datasheet_url = models.URLField(max_length=500, blank=True, default="")
    primary_image = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    search = models.GeneratedField(expression=SEARCH_VECTOR, output_field=SearchVectorField(), db_persist=True)

    class Meta:
        db_table = "catalog_component"
        ordering = ["sku", "id"]
        constraints = [
            models.UniqueConstraint(Lower("sku"), condition=LIVE, name="catalog_component_sku_live_uniq"),
            models.CheckConstraint(condition=Q(sku__regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$"), name="catalog_component_sku_format"),
            models.CheckConstraint(condition=Q(status__in=ComponentStatus.values), name="catalog_component_status_valid"),
            models.CheckConstraint(condition=Q(unit_override="") | Q(unit_override__in=Unit.values), name="catalog_component_unit_override_valid"),
            models.CheckConstraint(condition=Q(gst_rate_override__isnull=True) | (Q(gst_rate_override__gte=0) & Q(gst_rate_override__lte=1)), name="catalog_component_gst_override_fraction"),
            models.CheckConstraint(condition=Q(replacement__isnull=True) | ~Q(replacement=F("id")), name="catalog_component_not_own_replacement"),
            models.CheckConstraint(condition=~Q(name=""), name="catalog_component_name_not_blank"),
        ]
        indexes = [
            models.Index(fields=["category", "status"], name="catalog_comp_category_status"),
            GinIndex(fields=["search"], name="catalog_comp_search_gin"),
            GinIndex(fields=["attributes"], name="catalog_comp_attributes_gin"),
        ]

    def __str__(self) -> str:
        return f"{self.sku} — {self.name}"

    @property
    def effective_gst_rate(self):
        return self.gst_rate_override if self.gst_rate_override is not None else self.category.gst_rate

    @property
    def effective_unit(self) -> str:
        return self.unit_override or self.category.unit

    @property
    def effective_hsn_code(self) -> str:
        return self.hsn_code_override or self.category.hsn_code


class ComponentTier(BaseModel):
    """``catalog_component_tier``: the pack tiers a component is offered in (replaces ``bom.ItemTier``)."""

    # CASCADE: a tier row is a true child of its component.
    component = models.ForeignKey(Component, on_delete=models.CASCADE, related_name="tiers")
    tier = models.CharField(max_length=8, choices=Tier.choices)

    class Meta:
        db_table = "catalog_component_tier"
        ordering = ["component_id", "tier"]
        constraints = [
            models.UniqueConstraint(fields=["component", "tier"], condition=LIVE, name="catalog_component_tier_live_uniq"),
            models.CheckConstraint(condition=Q(tier__in=Tier.values), name="catalog_component_tier_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.component_id}/{self.tier}"


class ComponentChange(models.Model):
    """``catalog_component_change`` *(no base)*: per-field history for the Items & Prices screen (Flarize
    ``ComponentChangeLog`` semantics). Every change is also in ``audit_log``; this table avoids scanning partitions."""

    id = models.BigAutoField(primary_key=True)
    # CASCADE: the history belongs to the component (components are only soft-deleted in practice).
    component = models.ForeignKey(Component, on_delete=models.CASCADE, related_name="changes")
    at = models.DateTimeField(default=timezone.now)
    # SET_NULL: attribution only.
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    field = models.CharField(max_length=64)
    old = models.JSONField(null=True, blank=True)
    new = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "catalog_component_change"
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["component", "-at"], name="catalog_comp_change_comp_at")]

    def __str__(self) -> str:
        return f"{self.component_id}.{self.field} @ {self.at:%Y-%m-%d %H:%M}"
