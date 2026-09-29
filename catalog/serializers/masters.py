"""Brand, category and battery family shapes."""

from django.core.validators import RegexValidator
from rest_framework import serializers

from catalog.models import BatteryFamily, Brand, Category
from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer

SKU_PREFIX = RegexValidator(r"^[A-Za-z][A-Za-z0-9]{1,7}$", "2-8 letters or digits, starting with a letter (e.g. PNL).")


class BrandRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["uid", "name", "slug"]
        read_only_fields = fields


class CategoryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["uid", "slug", "name", "bom_role"]
        read_only_fields = fields


class BrandSerializer(serializers.ModelSerializer):
    logo = MediaAssetRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Brand
        fields = ["uid", "name", "slug", "country", "website", "logo", "is_active", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BrandWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    slug = serializers.SlugField(max_length=120, required=False, allow_blank=True, help_text="Derived from the name when blank.")
    country = serializers.CharField(max_length=64, required=False, allow_blank=True)
    website = serializers.URLField(max_length=200, required=False, allow_blank=True)
    logo = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), required=False, allow_null=True, help_text="Public image uid.")
    is_active = serializers.BooleanField(required=False)

    def to_representation(self, instance):
        return BrandSerializer(instance, context=self.context).data


class BrandUpdateSerializer(ExpectedVersionMixin, BrandWriteSerializer):
    name = serializers.CharField(max_length=100, required=False)


class CategorySerializer(serializers.ModelSerializer):
    spec_kind = serializers.SerializerMethodField(help_text="Spec table of its components: panel, inverter, battery, structure or null.")

    class Meta:
        model = Category
        fields = [
            "uid",
            "slug",
            "name",
            "bom_role",
            "spec_kind",
            "gst_rate",
            "hsn_code",
            "unit",
            "attribute_schema",
            "sku_prefix",
            "sort_order",
            "is_active",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def get_spec_kind(self, category) -> str | None:
        from catalog.services.specs import spec_kind

        return spec_kind(category)


class CategoryWriteSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=50)
    name = serializers.CharField(max_length=100)
    bom_role = serializers.ChoiceField(choices=Category._meta.get_field("bom_role").choices, required=False)
    gst_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=0, max_value=1, help_text="Fraction: 0.18 = 18 %.")
    hsn_code = serializers.CharField(max_length=12, required=False, allow_blank=True)
    unit = serializers.ChoiceField(choices=Category._meta.get_field("unit").choices, required=False)
    attribute_schema = serializers.JSONField(required=False, help_text='JSON Schema (draft 2020-12) with root {"type": "object"} for component.attributes.')
    sku_prefix = serializers.CharField(max_length=8, validators=[SKU_PREFIX], help_text="Generated SKUs are <PREFIX>-0001 …")
    sort_order = serializers.IntegerField(required=False)
    is_active = serializers.BooleanField(required=False)

    def to_representation(self, instance):
        return CategorySerializer(instance, context=self.context).data


class CategoryUpdateSerializer(ExpectedVersionMixin, CategoryWriteSerializer):
    slug = serializers.SlugField(max_length=50, required=False)
    name = serializers.CharField(max_length=100, required=False)
    gst_rate = serializers.DecimalField(max_digits=5, decimal_places=4, min_value=0, max_value=1, required=False)
    sku_prefix = serializers.CharField(max_length=8, validators=[SKU_PREFIX], required=False)


class BatteryFamilySerializer(serializers.ModelSerializer):
    class Meta:
        model = BatteryFamily
        fields = ["uid", "slug", "name", "voltage_class", "notes", "created_at", "updated_at", "version"]
        read_only_fields = fields


class BatteryFamilyWriteSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=64)
    name = serializers.CharField(max_length=120)
    voltage_class = serializers.ChoiceField(choices=BatteryFamily._meta.get_field("voltage_class").choices)
    notes = serializers.CharField(required=False, allow_blank=True)

    def to_representation(self, instance):
        return BatteryFamilySerializer(instance, context=self.context).data


class BatteryFamilyUpdateSerializer(ExpectedVersionMixin, BatteryFamilyWriteSerializer):
    slug = serializers.SlugField(max_length=64, required=False)
    name = serializers.CharField(max_length=120, required=False)
    voltage_class = serializers.ChoiceField(choices=BatteryFamily._meta.get_field("voltage_class").choices, required=False)


class VersionOnlySerializer(ExpectedVersionMixin, serializers.Serializer):
    """Body of workflow actions that take nothing but the optimistic-locking token."""
