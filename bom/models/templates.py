"""BOM templates, slots and fixed items (PLAN §2.5 ``bom_template``, ``bom_slot``, ``bom_fixed_item``).

A template describes the bill of materials of one system type: its sizes (the size keys ``3``, ``5sp``, ``5tp`` …
the market rates and packs speak), which of them are three-phase, the tiers and battery bands it offers, the
*slots* (one variable component per catalog category, chosen by tier/type/phase) and the *fixed items*
(consumables with their own reference price, or a catalog component).

Columns beyond the PLAN list (every legacy ``bom_bomtemplate``/``bom_bomslot``/``bom_bomfixeditem`` and Flarize
``bomTemplates`` field has a home; mapping in docs/decisions/bom.md, DV-87): template ``description``, ``sizes``,
``three_phase_sizes``, ``tiers``, ``battery_configs``; slot ``label``, ``gst_rate``, ``is_variable``, ``filter_type``,
``filter_phase``; fixed item ``code``, ``name``, ``unit_price``, ``gst_rate``, ``qty_rule``, ``section``, ``unit``,
``is_tube``, ``sort_order``. ``qty_rule`` / ``condition`` are validated documents (``bom.schemas``).
"""

from django.db import models
from django.db.models import Q

from bom.models.choices import LIVE, FilterPhase, FilterType, SystemType, in_choices
from core.models import BaseModel

KEY_PATTERN = r"^[a-z0-9][a-z0-9_]*$"


class Template(BaseModel):
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    sizes = models.JSONField(default=list, blank=True, help_text='Offered sizes in display order: [{"key": "5sp", "label": "5kW 1P"}, …].')
    three_phase_sizes = models.JSONField(default=list, blank=True, help_text="Size keys that are three-phase systems.")
    tiers = models.JSONField(default=list, blank=True, help_text="Offered tiers (lower case, source order).")
    battery_configs = models.JSONField(default=list, blank=True, help_text="Hybrid battery bands offered ('0', '1', '2').")

    class Meta:
        db_table = "bom_template"
        ordering = ["system_type", "id"]
        constraints = [
            models.UniqueConstraint(fields=["system_type"], condition=LIVE, name="bom_template_system_type_uniq"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="bom_template_system_type_valid"),
            models.CheckConstraint(condition=~Q(name=""), name="bom_template_name_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.system_type} — {self.name}"


class Slot(BaseModel):
    # CASCADE: a slot is a true child of its template.
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="slots")
    key = models.CharField(max_length=32, help_text="Slot key (the category slug for imported templates: panel, inverter, dcdb …).")
    # PROTECT: categories are catalog masters.
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="bom_slots")
    qty_rule = models.JSONField(help_text="Quantity rule document (bom.schemas.QTY_RULE_SCHEMA).")
    required = models.BooleanField(default=True, help_text="bom/build/ warns when a required slot needs a component and none is eligible.")
    sort_order = models.IntegerField(default=0, help_text="Line position (legacy BomSlot.pos).")
    label = models.CharField(max_length=100, blank=True, default="")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True, help_text="GST fraction for the line; null = the category's rate.")
    is_variable = models.BooleanField(default=True)
    filter_type = models.CharField(max_length=8, blank=True, default="", choices=FilterType.choices)
    filter_phase = models.CharField(max_length=8, blank=True, default="", choices=FilterPhase.choices)

    class Meta:
        db_table = "bom_slot"
        ordering = ["template_id", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["template", "key"], condition=LIVE, name="bom_slot_template_key_uniq"),
            models.CheckConstraint(condition=Q(key__regex=KEY_PATTERN), name="bom_slot_key_format"),
            models.CheckConstraint(condition=in_choices("filter_type", FilterType), name="bom_slot_filter_type_valid"),
            models.CheckConstraint(condition=in_choices("filter_phase", FilterPhase), name="bom_slot_filter_phase_valid"),
            models.CheckConstraint(condition=Q(gst_rate__isnull=True) | (Q(gst_rate__gte=0) & Q(gst_rate__lte=1)), name="bom_slot_gst_rate_fraction"),
        ]
        indexes = [models.Index(fields=["template", "sort_order"], name="bom_slot_template_order")]

    def __str__(self) -> str:
        return f"{self.template_id}/{self.key}"


class FixedItem(BaseModel):
    # CASCADE: a fixed item is a true child of its template.
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="fixed_items")
    # PROTECT: components and categories are catalog masters.
    component = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="bom_fixed_items")
    category = models.ForeignKey("catalog.Category", null=True, blank=True, on_delete=models.PROTECT, related_name="bom_fixed_items")
    code = models.CharField(max_length=64, blank=True, default="", help_text="Source identifier (Flarize fixed item id, e.g. fi_mc4_connector).")
    name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="Reference price of a template consumable; null = the component's current LIST price.")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True, help_text="GST fraction; null = the category's rate (18 % without one).")
    qty = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Constant quantity (when there is no qty_rule).")
    qty_rule = models.JSONField(null=True, blank=True, help_text="Quantity rule document (bom.schemas.QTY_RULE_SCHEMA); wins over qty.")
    condition = models.JSONField(default=dict, blank=True, help_text="When the item applies (bom.schemas.CONDITION_SCHEMA); {} = always.")
    section = models.CharField(max_length=32, blank=True, default="", help_text="Quote section / upgrade section (battery, hybridInv …).")
    unit = models.CharField(max_length=12, blank=True, default="")
    is_tube = models.BooleanField(default=False, help_text="Priced through the structure cost, never as a BOM line.")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "bom_fixed_item"
        ordering = ["template_id", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=~Q(name=""), name="bom_fixed_item_name_not_blank"),
            models.CheckConstraint(condition=Q(qty__isnull=False) | Q(qty_rule__isnull=False), name="bom_fixed_item_has_quantity"),
            models.CheckConstraint(condition=Q(qty__isnull=True) | Q(qty__gte=0), name="bom_fixed_item_qty_not_negative"),
            models.CheckConstraint(condition=Q(unit_price__isnull=True) | Q(unit_price__gte=0), name="bom_fixed_item_price_not_negative"),
            models.CheckConstraint(condition=Q(unit_price__isnull=False) | Q(component__isnull=False), name="bom_fixed_item_priced"),
            models.CheckConstraint(condition=Q(gst_rate__isnull=True) | (Q(gst_rate__gte=0) & Q(gst_rate__lte=1)), name="bom_fixed_item_gst_rate_fraction"),
        ]
        indexes = [models.Index(fields=["template", "sort_order"], name="bom_fixed_item_template_order")]

    def __str__(self) -> str:
        return f"{self.template_id}/{self.name}"
