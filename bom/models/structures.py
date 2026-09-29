"""Structure templates and tube weights (PLAN §2.5 ``bom_structure_template``, ``bom_structure_template_item``,
``bom_tube_weight``).

A structure template is the mounting kit of one roof type (``flat_roof``, ``elevated``, ``sheet_roof``): tubes priced by
weight (kg × the GP/GI rate per kg of the tier) and fixed items priced per unit, each with a quantity rule over the
system kW (``kw_interpolated``: the legacy ``getStructureQty`` interpolation).

Columns beyond the PLAN list (DV-87): item ``name``, ``item_type``, ``weight_kg`` (weight of one tube length, the legacy
``weight_kg``), ``unit_price``, ``unit``, ``sort_order``; the tube weight is ``weight_kg`` per tube length (PLAN
``kg_per_m``: the legacy values — 15.3 kg for a 2.5×1.5 16G tube — are per 6 m length, not per metre).
"""

from django.db import models
from django.db.models import Q

from bom.models.choices import LIVE, StructureItemType, in_choices
from core.models import BaseModel

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_]*$"


class StructureTemplate(BaseModel):
    slug = models.CharField(max_length=32, help_text="flat_roof, elevated, sheet_roof …")
    name = models.CharField(max_length=100)
    labour_rate_key = models.CharField(max_length=48, blank=True, default="", help_text="pricing_cost_config key of the roof type's rate (e.g. elevated_structure_rate).")

    class Meta:
        db_table = "bom_structure_template"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="bom_structure_template_slug_uniq"),
            models.CheckConstraint(condition=Q(slug__regex=SLUG_PATTERN), name="bom_structure_template_slug_format"),
            models.CheckConstraint(condition=~Q(name=""), name="bom_structure_template_name_not_blank"),
        ]

    def __str__(self) -> str:
        return self.slug


class StructureTemplateItem(BaseModel):
    # CASCADE: an item is a true child of its structure template.
    template = models.ForeignKey(StructureTemplate, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=255)
    item_type = models.CharField(max_length=8, choices=StructureItemType.choices)
    tube_size = models.CharField(max_length=12, blank=True, default="")
    weight_kg = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True, help_text="TUBE: weight of one tube length in kg.")
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="FIXED: price per unit.")
    unit = models.CharField(max_length=12, blank=True, default="")
    length_m_per_kw = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, help_text="Tube metres per kW (PLAN; not used by the legacy sources).")
    qty_rule = models.JSONField(help_text="Quantity rule document (bom.schemas.QTY_RULE_SCHEMA), usually kw_interpolated.")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "bom_structure_template_item"
        ordering = ["template_id", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=in_choices("item_type", StructureItemType), name="bom_structure_item_type_valid"),
            models.CheckConstraint(condition=~Q(name=""), name="bom_structure_item_name_not_blank"),
            models.CheckConstraint(condition=Q(weight_kg__isnull=True) | Q(weight_kg__gte=0), name="bom_structure_item_weight_not_negative"),
            models.CheckConstraint(condition=Q(unit_price__isnull=True) | Q(unit_price__gte=0), name="bom_structure_item_price_not_negative"),
            models.CheckConstraint(condition=Q(length_m_per_kw__isnull=True) | Q(length_m_per_kw__gte=0), name="bom_structure_item_length_not_negative"),
        ]
        indexes = [models.Index(fields=["template", "sort_order"], name="bom_structure_item_order")]

    def __str__(self) -> str:
        return f"{self.template_id}/{self.name}"


class TubeWeight(BaseModel):
    tube_size = models.CharField(max_length=12)
    weight_kg = models.DecimalField(max_digits=8, decimal_places=4, help_text="Weight of one tube length in kg.")

    class Meta:
        db_table = "bom_tube_weight"
        ordering = ["tube_size", "id"]
        constraints = [
            models.UniqueConstraint(fields=["tube_size"], condition=LIVE, name="bom_tube_weight_size_uniq"),
            models.CheckConstraint(condition=~Q(tube_size=""), name="bom_tube_weight_size_not_blank"),
            models.CheckConstraint(condition=Q(weight_kg__gt=0), name="bom_tube_weight_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.tube_size} = {self.weight_kg} kg"
