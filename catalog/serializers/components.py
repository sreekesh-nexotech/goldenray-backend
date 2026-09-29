"""Component shapes: read (with nested refs, tiers and the spec of the category's kind), write, workflow actions,
history and usage."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog.models import Brand, Category, Component, ComponentChange, Tier
from catalog.serializers.masters import BrandRefSerializer, CategoryRefSerializer
from catalog.serializers.specs import (
    BatterySpecSerializer,
    BatterySpecWriteSerializer,
    InverterSpecSerializer,
    InverterSpecWriteSerializer,
    PanelSpecSerializer,
    PanelSpecWriteSerializer,
    StructureSpecSerializer,
    StructureSpecWriteSerializer,
)
from catalog.services.components import live_tiers
from catalog.services.specs import get_spec, spec_kind
from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer


class ComponentRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["uid", "sku", "name", "status"]
        read_only_fields = fields


class ProfileRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    slug = serializers.CharField()
    status = serializers.CharField()


class ComponentSerializer(serializers.ModelSerializer):
    category = CategoryRefSerializer(read_only=True)
    brand = BrandRefSerializer(read_only=True, allow_null=True)
    replacement = ComponentRefSerializer(read_only=True, allow_null=True)
    datasheet = MediaAssetRefSerializer(read_only=True, allow_null=True)
    primary_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    tiers = serializers.SerializerMethodField()
    spec_kind = serializers.SerializerMethodField(help_text="Which of the *_spec fields applies (null: the category has no spec table).")
    panel_spec = serializers.SerializerMethodField()
    inverter_spec = serializers.SerializerMethodField()
    battery_spec = serializers.SerializerMethodField()
    structure_spec = serializers.SerializerMethodField()
    public_profile = serializers.SerializerMethodField()
    effective_gst_rate = serializers.DecimalField(max_digits=5, decimal_places=4, read_only=True)
    effective_unit = serializers.CharField(read_only=True)
    effective_hsn_code = serializers.CharField(read_only=True)

    class Meta:
        model = Component
        fields = [
            "uid",
            "sku",
            "category",
            "brand",
            "brand_label",
            "name",
            "model",
            "description",
            "attributes",
            "gst_rate_override",
            "hsn_code_override",
            "unit_override",
            "effective_gst_rate",
            "effective_unit",
            "effective_hsn_code",
            "status",
            "status_changed_at",
            "deprecated_reason",
            "retired_reason",
            "replacement",
            "is_public",
            "is_premium",
            "warranty_product_years",
            "warranty_performance_years",
            "warranty_extendable_years",
            "warranty_text",
            "engineering_status",
            "notes",
            "datasheet",
            "datasheet_url",
            "primary_image",
            "tiers",
            "spec_kind",
            "panel_spec",
            "inverter_spec",
            "battery_spec",
            "structure_spec",
            "public_profile",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=Tier.choices)))
    def get_tiers(self, component) -> list[str]:
        return live_tiers(component)

    def get_spec_kind(self, component) -> str | None:
        return spec_kind(component.category)

    def _spec(self, component, kind, serializer_class):
        if spec_kind(component.category) != kind:
            return None
        spec = get_spec(component, kind)
        return serializer_class(spec).data if spec is not None else None

    @extend_schema_field(PanelSpecSerializer(allow_null=True))
    def get_panel_spec(self, component):
        return self._spec(component, "panel", PanelSpecSerializer)

    @extend_schema_field(InverterSpecSerializer(allow_null=True))
    def get_inverter_spec(self, component):
        return self._spec(component, "inverter", InverterSpecSerializer)

    @extend_schema_field(BatterySpecSerializer(allow_null=True))
    def get_battery_spec(self, component):
        return self._spec(component, "battery", BatterySpecSerializer)

    @extend_schema_field(StructureSpecSerializer(allow_null=True))
    def get_structure_spec(self, component):
        return self._spec(component, "structure", StructureSpecSerializer)

    @extend_schema_field(ProfileRefSerializer(allow_null=True))
    def get_public_profile(self, component):
        profile = getattr(component, "public_profile", None)
        if profile is None or profile.deleted_at is not None:
            return None
        return {"uid": profile.uid, "slug": profile.slug, "status": profile.status}


class ComponentWriteSerializer(serializers.Serializer):
    category = serializers.SlugRelatedField(slug_field="uid", queryset=Category.objects.all(), help_text="Category uid.")
    brand = serializers.SlugRelatedField(slug_field="uid", queryset=Brand.objects.all(), required=False, allow_null=True, help_text="Brand uid (null: unbranded).")
    brand_label = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Brand as printed; defaults to the brand's name.")
    sku = serializers.CharField(max_length=32, required=False, allow_blank=True, help_text="Generated (<PREFIX>-0001) when blank; fixed once the component leaves DRAFT.")
    name = serializers.CharField(max_length=255)
    model = serializers.CharField(max_length=120, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    attributes = serializers.DictField(required=False, help_text="Validated against the category's attribute_schema.")
    gst_rate_override = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=0, max_value=1, required=False, allow_null=True)
    hsn_code_override = serializers.CharField(max_length=12, required=False, allow_blank=True)
    unit_override = serializers.ChoiceField(choices=[("", "Category unit"), *Component._meta.get_field("unit_override").choices], required=False, allow_blank=True)
    is_public = serializers.BooleanField(required=False)
    is_premium = serializers.BooleanField(required=False)
    warranty_product_years = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    warranty_performance_years = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    warranty_extendable_years = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    warranty_text = serializers.CharField(max_length=64, required=False, allow_blank=True)
    engineering_status = serializers.CharField(max_length=48, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    datasheet = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), required=False, allow_null=True, help_text="Public DOCUMENT uid.")
    datasheet_url = serializers.URLField(max_length=500, required=False, allow_blank=True)
    primary_image = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), required=False, allow_null=True, help_text="Public IMAGE uid.")
    tiers = serializers.ListField(child=serializers.ChoiceField(choices=Tier.choices), required=False, max_length=3)
    panel_spec = PanelSpecWriteSerializer(required=False, allow_null=True)
    inverter_spec = InverterSpecWriteSerializer(required=False, allow_null=True)
    battery_spec = BatterySpecWriteSerializer(required=False, allow_null=True)
    structure_spec = StructureSpecWriteSerializer(required=False, allow_null=True)

    def to_representation(self, instance):
        return ComponentSerializer(instance, context=self.context).data


class ComponentUpdateSerializer(ExpectedVersionMixin, ComponentWriteSerializer):
    category = serializers.SlugRelatedField(slug_field="uid", queryset=Category.objects.all(), required=False)
    name = serializers.CharField(max_length=255, required=False)


class DeprecateSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    replacement_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.objects.all(), required=False, allow_null=True, help_text="Suggested replacement (same category).")


class LifecycleActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class ChangeActorSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField()


class ComponentChangeSerializer(serializers.ModelSerializer):
    by = serializers.SerializerMethodField()
    old = serializers.JSONField(allow_null=True)
    new = serializers.JSONField(allow_null=True)

    class Meta:
        model = ComponentChange
        fields = ["at", "by", "field", "old", "new", "reason"]
        read_only_fields = fields

    @extend_schema_field(ChangeActorSerializer(allow_null=True))
    def get_by(self, change):
        if change.by is None:
            return None
        return {"uid": change.by.uid, "name": change.by.get_full_name() or change.by.email}


class UsageReferenceSerializer(serializers.Serializer):
    object_type = serializers.CharField()
    object_uid = serializers.CharField(allow_null=True)
    label = serializers.CharField(allow_blank=True)
    status = serializers.CharField(allow_blank=True)


class UsageSectionSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Provider, e.g. packs.config_lines.")
    count = serializers.IntegerField()
    references = UsageReferenceSerializer(many=True, help_text="At most 100 per provider.")
    error = serializers.BooleanField(help_text="The provider failed; deletion is refused until it answers.")


class UsageSerializer(serializers.Serializer):
    component = ComponentRefSerializer()
    in_use = serializers.BooleanField()
    total = serializers.IntegerField()
    sections = UsageSectionSerializer(many=True)
