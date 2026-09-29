"""Serializers of the bom configuration CRUD endpoints (read shapes, create and PATCH bodies)."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from bom.models import FixedItem, PackageProfile, Slot, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from catalog.models import Category, Component
from core.serializers.common import ExpectedVersionMixin

QTY_RULE_HELP = "Quantity rule document (see bom.schemas: size_table, fixed, new_panels, upgrade_path, kw_interpolated, per_kw, per_panel, by_phase)."
FRACTION = {"max_digits": 5, "decimal_places": 4, "min_value": Decimal("0"), "max_value": Decimal("1")}


class BomCategoryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["uid", "slug", "name"]
        read_only_fields = fields


class BomComponentRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["uid", "sku", "name", "status"]
        read_only_fields = fields


def _ref(model, source: str, *, required: bool = True, allow_null: bool = False):
    return serializers.SlugRelatedField(slug_field="uid", queryset=model.objects.all(), source=source, required=required, allow_null=allow_null)


class _Write(serializers.Serializer):
    read_serializer: type[serializers.Serializer]

    def to_representation(self, instance):
        return self.read_serializer(instance, context=self.context).data


def _optional(serializer_class: type[serializers.Serializer], *keep_required: str) -> type[serializers.Serializer]:
    """The PATCH body: every field of the create body optional (and ``expected_version``)."""
    fields = {}
    for name, field in serializer_class().get_fields().items():
        if name in keep_required:
            continue
        kwargs = dict(field._kwargs)
        kwargs["required"] = False
        kwargs.pop("default", None)
        fields[name] = type(field)(*field._args, **kwargs)
    return type(f"{serializer_class.__name__.replace('Write', 'Update')}", (ExpectedVersionMixin, _Write), {**fields, "read_serializer": serializer_class.read_serializer})


# ── templates ──


class BomTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Template
        fields = ["uid", "system_type", "name", "description", "is_active", "sizes", "three_phase_sizes", "tiers", "battery_configs", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BomTemplateWriteSerializer(_Write):
    read_serializer = BomTemplateSerializer
    system_type = serializers.ChoiceField(choices=Template._meta.get_field("system_type").choices)
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)
    sizes = serializers.JSONField(required=False, default=list, help_text='[{"key": "5sp", "label": "5kW 1P"}, …]')
    three_phase_sizes = serializers.JSONField(required=False, default=list)
    tiers = serializers.JSONField(required=False, default=list)
    battery_configs = serializers.JSONField(required=False, default=list)


BomTemplateUpdateSerializer = _optional(BomTemplateWriteSerializer)


# ── slots ──


class BomSlotSerializer(serializers.ModelSerializer):
    template_uid = serializers.UUIDField(source="template.uid", read_only=True)
    category = BomCategoryRefSerializer(read_only=True)

    class Meta:
        model = Slot
        fields = [
            "uid",
            "template_uid",
            "key",
            "category",
            "qty_rule",
            "required",
            "sort_order",
            "label",
            "gst_rate",
            "is_variable",
            "filter_type",
            "filter_phase",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class BomSlotWriteSerializer(_Write):
    read_serializer = BomSlotSerializer
    template_uid = _ref(Template, "template")
    key = serializers.RegexField(r"^[a-z0-9][a-z0-9_]*$", max_length=32)
    category_uid = _ref(Category, "category")
    qty_rule = serializers.JSONField(help_text=QTY_RULE_HELP)
    required = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False, default=0)
    label = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    gst_rate = serializers.DecimalField(**FRACTION, required=False, allow_null=True, default=None)
    is_variable = serializers.BooleanField(required=False, default=True)
    filter_type = serializers.ChoiceField(choices=Slot._meta.get_field("filter_type").choices, required=False, allow_blank=True, default="")
    filter_phase = serializers.ChoiceField(choices=Slot._meta.get_field("filter_phase").choices, required=False, allow_blank=True, default="")


BomSlotUpdateSerializer = _optional(BomSlotWriteSerializer)
del BomSlotUpdateSerializer._declared_fields["template_uid"]


# ── fixed items ──


class BomFixedItemSerializer(serializers.ModelSerializer):
    template_uid = serializers.UUIDField(source="template.uid", read_only=True)
    component = BomComponentRefSerializer(read_only=True, allow_null=True)
    category = BomCategoryRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = FixedItem
        fields = [
            "uid",
            "template_uid",
            "component",
            "category",
            "code",
            "name",
            "unit_price",
            "gst_rate",
            "qty",
            "qty_rule",
            "condition",
            "section",
            "unit",
            "is_tube",
            "sort_order",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class BomFixedItemWriteSerializer(_Write):
    read_serializer = BomFixedItemSerializer
    template_uid = _ref(Template, "template")
    component_uid = _ref(Component, "component", required=False, allow_null=True)
    category_uid = _ref(Category, "category", required=False, allow_null=True)
    code = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    name = serializers.CharField(max_length=255)
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    gst_rate = serializers.DecimalField(**FRACTION, required=False, allow_null=True, default=None)
    qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    qty_rule = serializers.JSONField(required=False, allow_null=True, default=None, help_text=QTY_RULE_HELP)
    condition = serializers.JSONField(required=False, default=dict, help_text="Filters (sizes, phases, tiers, battery_configs, min_kw, max_kw); {} = always.")
    section = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    unit = serializers.CharField(max_length=12, required=False, allow_blank=True, default="")
    is_tube = serializers.BooleanField(required=False, default=False)
    sort_order = serializers.IntegerField(required=False, default=0)


BomFixedItemUpdateSerializer = _optional(BomFixedItemWriteSerializer)
del BomFixedItemUpdateSerializer._declared_fields["template_uid"]


# ── structure templates, items, tube weights ──


class BomStructureTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = StructureTemplate
        fields = ["uid", "slug", "name", "labour_rate_key", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BomStructureTemplateWriteSerializer(_Write):
    read_serializer = BomStructureTemplateSerializer
    slug = serializers.RegexField(r"^[a-z0-9][a-z0-9_]*$", max_length=32)
    name = serializers.CharField(max_length=100)
    labour_rate_key = serializers.CharField(max_length=48, required=False, allow_blank=True, default="")


BomStructureTemplateUpdateSerializer = _optional(BomStructureTemplateWriteSerializer)


class BomStructureItemSerializer(serializers.ModelSerializer):
    template_uid = serializers.UUIDField(source="template.uid", read_only=True)

    class Meta:
        model = StructureTemplateItem
        fields = ["uid", "template_uid", "name", "item_type", "tube_size", "weight_kg", "unit_price", "unit", "length_m_per_kw", "qty_rule", "sort_order", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BomStructureItemWriteSerializer(_Write):
    read_serializer = BomStructureItemSerializer
    template_uid = _ref(StructureTemplate, "template")
    name = serializers.CharField(max_length=255)
    item_type = serializers.ChoiceField(choices=StructureTemplateItem._meta.get_field("item_type").choices)
    tube_size = serializers.CharField(max_length=12, required=False, allow_blank=True, default="")
    weight_kg = serializers.DecimalField(max_digits=8, decimal_places=4, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    unit = serializers.CharField(max_length=12, required=False, allow_blank=True, default="")
    length_m_per_kw = serializers.DecimalField(max_digits=8, decimal_places=3, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    qty_rule = serializers.JSONField(help_text=QTY_RULE_HELP)
    sort_order = serializers.IntegerField(required=False, default=0)


BomStructureItemUpdateSerializer = _optional(BomStructureItemWriteSerializer)
del BomStructureItemUpdateSerializer._declared_fields["template_uid"]


class BomTubeWeightSerializer(serializers.ModelSerializer):
    class Meta:
        model = TubeWeight
        fields = ["uid", "tube_size", "weight_kg", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BomTubeWeightWriteSerializer(_Write):
    read_serializer = BomTubeWeightSerializer
    tube_size = serializers.CharField(max_length=12)
    weight_kg = serializers.DecimalField(max_digits=8, decimal_places=4, min_value=Decimal("0.0001"))


BomTubeWeightUpdateSerializer = _optional(BomTubeWeightWriteSerializer)


# ── package profiles ──


class BomPackageProfileSerializer(serializers.ModelSerializer):
    battery_component = BomComponentRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = PackageProfile
        fields = [
            "uid",
            "key",
            "label",
            "structure_material",
            "inverter_type",
            "battery_included",
            "battery_brand",
            "battery_model",
            "battery_capacity_kwh",
            "battery_quantity",
            "battery_component",
            "structure_labor_override",
            "repair_margin_override",
            "notes",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class BomPackageProfileWriteSerializer(_Write):
    read_serializer = BomPackageProfileSerializer
    key = serializers.RegexField(r"^[a-z0-9][a-z0-9_]*$", max_length=32)
    label = serializers.CharField(max_length=100)
    structure_material = serializers.ChoiceField(choices=PackageProfile._meta.get_field("structure_material").choices)
    inverter_type = serializers.ChoiceField(choices=PackageProfile._meta.get_field("inverter_type").choices)
    battery_included = serializers.BooleanField(required=False, default=False)
    battery_brand = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    battery_model = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    battery_capacity_kwh = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0"), required=False, default=Decimal("0"))
    battery_quantity = serializers.IntegerField(min_value=0, max_value=2, required=False, default=0)
    battery_component_uid = _ref(Component, "battery_component", required=False, allow_null=True)
    structure_labor_override = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True, default=None)
    repair_margin_override = serializers.DecimalField(max_digits=7, decimal_places=4, min_value=Decimal("0"), max_value=Decimal("1"), required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


BomPackageProfileUpdateSerializer = _optional(BomPackageProfileWriteSerializer)
