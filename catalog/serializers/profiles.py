"""Public profile shapes: staff (``catalog/public-profiles/``) and website (``products/…``)."""

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from catalog.models import RATING_KEYS, Component, ComponentPublicProfile
from catalog.serializers.components import ComponentRefSerializer
from catalog.serializers.masters import BrandRefSerializer, CategoryRefSerializer
from catalog.serializers.specs import BatteryFamilyRefSerializer
from catalog.services import pricing_hooks
from catalog.services.public import gallery_assets
from catalog.services.specs import get_spec, spec_kind
from core.serializers.common import ExpectedVersionMixin
from media.serializers import MediaAssetRefSerializer


class RatingsSerializer(serializers.Serializer):
    efficiency = serializers.IntegerField(min_value=0, max_value=100, required=False)
    heat_performance = serializers.IntegerField(min_value=0, max_value=100, required=False)
    reliability = serializers.IntegerField(min_value=0, max_value=100, required=False)
    warranty = serializers.IntegerField(min_value=0, max_value=100, required=False)
    kerala_climate = serializers.IntegerField(min_value=0, max_value=100, required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(RATING_KEYS))
            if unknown:
                raise serializers.ValidationError({key: ["Unknown rating; use " + ", ".join(RATING_KEYS) + "."] for key in unknown})
        return super().to_internal_value(data)


# A question/answer pair of a public profile; the component name leaves ``Faq`` to the faqs app's FAQ resource.
@extend_schema_serializer(component_name="CatalogProfileFaq")
class FaqSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=500)
    answer = serializers.CharField(max_length=5000)


class PublicProfileSerializer(serializers.ModelSerializer):
    component = ComponentRefSerializer(read_only=True)
    ratings = RatingsSerializer(read_only=True)
    pros = serializers.ListField(child=serializers.CharField(), read_only=True)
    cons = serializers.ListField(child=serializers.CharField(), read_only=True)
    faq = FaqSerializer(many=True, read_only=True)
    gallery = serializers.ListField(child=serializers.UUIDField(), read_only=True)

    class Meta:
        model = ComponentPublicProfile
        fields = [
            "uid",
            "component",
            "slug",
            "headline",
            "summary",
            "body",
            "image_url",
            "price_range_label",
            "subsidy_eligible",
            "kerala_climate_score",
            "overall_rating",
            "rating_tier",
            "ratings",
            "pros",
            "cons",
            "faq",
            "gallery",
            "seo_title",
            "seo_description",
            "status",
            "published_at",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class PublicProfileWriteSerializer(serializers.Serializer):
    component_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.objects.all(), source="component", help_text="The component this profile markets.")
    slug = serializers.SlugField(max_length=160, required=False, allow_blank=True, help_text="Derived from brand + model when blank.")
    headline = serializers.CharField(max_length=200, required=False, allow_blank=True)
    summary = serializers.CharField(required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True, help_text="Markdown.")
    image_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    price_range_label = serializers.CharField(max_length=100, required=False, allow_blank=True)
    subsidy_eligible = serializers.BooleanField(required=False, allow_null=True)
    kerala_climate_score = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    overall_rating = serializers.ChoiceField(choices=ComponentPublicProfile._meta.get_field("overall_rating").choices, required=False, allow_null=True)
    rating_tier = serializers.ChoiceField(choices=ComponentPublicProfile._meta.get_field("rating_tier").choices, required=False, allow_null=True)
    ratings = RatingsSerializer(required=False)
    pros = serializers.ListField(child=serializers.CharField(max_length=300), required=False, max_length=30)
    cons = serializers.ListField(child=serializers.CharField(max_length=300), required=False, max_length=30)
    faq = FaqSerializer(many=True, required=False, max_length=50)
    gallery = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=30, help_text="Public image uids, in display order.")
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    seo_description = serializers.CharField(required=False, allow_blank=True)

    def validate_ratings(self, value):
        return dict(value)

    def validate_faq(self, value):
        return [dict(item) for item in value]

    def validate_gallery(self, value):
        return [str(item) for item in value]

    def to_representation(self, instance):
        return PublicProfileSerializer(instance, context=self.context).data


class PublicProfileUpdateSerializer(ExpectedVersionMixin, PublicProfileWriteSerializer):
    component_uid = None
    slug = serializers.SlugField(max_length=160, required=False)


# ── website ────────────────────────────────────────────────────────────────────────────────────────────────────


class PriceSerializer(serializers.Serializer):
    min = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    max = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField()
    gst_inclusive = serializers.BooleanField()
    release = serializers.IntegerField(allow_null=True, help_text="PriceRelease number the price comes from.")


class WarrantySerializer(serializers.Serializer):
    product_years = serializers.IntegerField(allow_null=True)
    performance_years = serializers.IntegerField(allow_null=True)
    extendable_years = serializers.IntegerField(allow_null=True)
    text = serializers.CharField(allow_blank=True)


_PUBLIC_SPEC_EXCLUDE = {
    "inverter": {"system_controller_required"},
    "battery": {
        "engineering_status",
        "procurement_status",
        "engineering_notes",
        "open_items",
        "status_history",
        "supplier",
        "supplier_reference",
        "compatible_inverters",
        "architecture",
    },
}


def public_spec(component) -> dict | None:
    from catalog.serializers.specs import READ_SERIALIZERS

    kind = spec_kind(component.category)
    spec = get_spec(component, kind) if kind else None
    if spec is None:
        return None
    data = dict(READ_SERIALIZERS[kind](spec).data)
    for name in _PUBLIC_SPEC_EXCLUDE.get(kind, ()):
        data.pop(name, None)
    if kind == "battery":
        data["family"] = BatteryFamilyRefSerializer(spec.family).data if spec.family_id else None
    return data


class PublicProductSerializer(serializers.ModelSerializer):
    """One product card: marketing profile + spec + price (``context["prices"]``: ``{component pk: PriceInfo}``)."""

    sku = serializers.CharField(source="component.sku")
    name = serializers.CharField(source="component.name")
    model = serializers.CharField(source="component.model")
    category = CategoryRefSerializer(source="component.category")
    brand = BrandRefSerializer(source="component.brand", allow_null=True)
    brand_label = serializers.CharField(source="component.brand_label")
    status = serializers.CharField(source="component.status", help_text="ACTIVE or DEPRECATED.")
    image = serializers.SerializerMethodField()
    ratings = RatingsSerializer()
    warranty = serializers.SerializerMethodField()
    spec_kind = serializers.SerializerMethodField()
    spec = serializers.SerializerMethodField(help_text="Columns of the category's spec table (panel, inverter, battery or structure).")
    price = serializers.SerializerMethodField()
    price_range_label = serializers.SerializerMethodField(help_text="From the current price release, else the profile's label.")

    class Meta:
        model = ComponentPublicProfile
        fields = [
            "slug",
            "sku",
            "name",
            "model",
            "category",
            "brand",
            "brand_label",
            "status",
            "headline",
            "summary",
            "image",
            "image_url",
            "price",
            "price_range_label",
            "subsidy_eligible",
            "kerala_climate_score",
            "overall_rating",
            "rating_tier",
            "ratings",
            "warranty",
            "spec_kind",
            "spec",
            "published_at",
            "updated_at",
        ]
        read_only_fields = fields

    def _price(self, profile):
        return (self.context.get("prices") or {}).get(profile.component_id)

    @extend_schema_field(MediaAssetRefSerializer(allow_null=True))
    def get_image(self, profile):
        asset = profile.component.primary_image
        if asset is None or asset.deleted_at is not None or not asset.is_public:
            return None
        return MediaAssetRefSerializer(asset).data

    @extend_schema_field(WarrantySerializer)
    def get_warranty(self, profile):
        component = profile.component
        return {
            "product_years": component.warranty_product_years,
            "performance_years": component.warranty_performance_years,
            "extendable_years": component.warranty_extendable_years,
            "text": component.warranty_text,
        }

    def get_spec_kind(self, profile) -> str | None:
        return spec_kind(profile.component.category)

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_spec(self, profile):
        return public_spec(profile.component)

    @extend_schema_field(PriceSerializer(allow_null=True))
    def get_price(self, profile):
        info = self._price(profile)
        if info is None or (info.min_amount is None and info.max_amount is None):
            return None
        return PriceSerializer({"min": info.min_amount, "max": info.max_amount, "currency": info.currency, "gst_inclusive": info.gst_inclusive, "release": info.release_number}).data

    def get_price_range_label(self, profile) -> str:
        return pricing_hooks.price_label(self._price(profile), profile.price_range_label)


class PublicProductDetailSerializer(PublicProductSerializer):
    pros = serializers.ListField(child=serializers.CharField())
    cons = serializers.ListField(child=serializers.CharField())
    faq = FaqSerializer(many=True)
    gallery = serializers.SerializerMethodField()
    datasheet = serializers.SerializerMethodField()
    seo = serializers.SerializerMethodField()
    description = serializers.CharField(source="component.description")

    class Meta(PublicProductSerializer.Meta):
        fields = [*PublicProductSerializer.Meta.fields, "description", "body", "pros", "cons", "faq", "gallery", "datasheet", "seo"]
        read_only_fields = fields

    @extend_schema_field(MediaAssetRefSerializer(many=True))
    def get_gallery(self, profile):
        return MediaAssetRefSerializer(gallery_assets(profile), many=True).data

    @extend_schema_field(serializers.URLField(allow_null=True, help_text="Public datasheet (media) or the recorded datasheet URL."))
    def get_datasheet(self, profile):
        component = profile.component
        asset = component.datasheet
        if asset is not None and asset.deleted_at is None and asset.is_public:
            return asset.cdn_url or None
        return component.datasheet_url or None

    @extend_schema_field(serializers.DictField(child=serializers.CharField()))
    def get_seo(self, profile):
        return {"title": profile.seo_title or profile.headline or profile.component.name, "description": profile.seo_description or profile.summary[:300]}
