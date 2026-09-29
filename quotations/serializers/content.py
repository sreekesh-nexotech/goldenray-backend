"""Quotation content shapes (staff) and the public testimonial."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from media.models import MediaAsset
from quotations.models import Campaign, ContentVersion, Inclusion, InclusionKind, SystemType, Testimonial, Tier, TierDisplayName


class ContentVersionSerializer(serializers.ModelSerializer):
    published_by = serializers.UUIDField(source="published_by.uid", read_only=True, allow_null=True)
    fit_ok = serializers.SerializerMethodField()

    class Meta:
        model = ContentVersion
        fields = ["uid", "number", "status", "note", "fit_ok", "published_at", "published_by", "superseded_at", "release_sha256", "created_at", "updated_at", "version"]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_fit_ok(self, row):
        return (row.fit_report or {}).get("ok")


class ContentVersionDetailSerializer(ContentVersionSerializer):
    class Meta(ContentVersionSerializer.Meta):
        fields = [*ContentVersionSerializer.Meta.fields, "language_payload", "fit_report", "release_payload"]
        read_only_fields = fields


class ContentVersionCreateSerializer(serializers.Serializer):
    language_payload = serializers.JSONField(required=False, help_text="Bilingual content (every text {en, ml}); omitted: a copy of the published content.")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ContentVersionUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    language_payload = serializers.JSONField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class FitCheckSerializer(serializers.Serializer):
    language_payload = serializers.JSONField(required=False, help_text="A candidate to check without saving; omitted: the stored content (the report is kept on a draft).")


class FitReportSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    errors = serializers.ListField(child=serializers.DictField())
    bySection = serializers.DictField()


class ContentPublishSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class _Versioned(ExpectedVersionMixin, serializers.ModelSerializer):
    pass


class InclusionSerializer(_Versioned):
    kind = serializers.ChoiceField(choices=InclusionKind.choices, required=False)

    class Meta:
        model = Inclusion
        fields = ["uid", "key", "kind", "label_en", "label_ml", "default_on", "applies_to", "sort_order", "created_at", "updated_at", "version", "expected_version"]
        read_only_fields = ["uid", "created_at", "updated_at", "version"]


class TierDisplayNameSerializer(_Versioned):
    system_type = serializers.ChoiceField(choices=SystemType.choices)
    tier = serializers.ChoiceField(choices=Tier.choices)

    class Meta:
        model = TierDisplayName
        fields = ["uid", "system_type", "tier", "name_en", "name_ml", "is_recommended", "badge_en", "badge_ml", "created_at", "updated_at", "version", "expected_version"]
        read_only_fields = ["uid", "created_at", "updated_at", "version"]


class PublicImageField(serializers.SlugRelatedField):
    """A live media asset by ``uid`` (the service checks it is a public image)."""

    def __init__(self, **kwargs):
        super().__init__(slug_field="uid", queryset=MediaAsset.objects.all(), **kwargs)


class TestimonialSerializer(_Versioned):
    photo = PublicImageField(required=False, allow_null=True)
    photo_src = serializers.SerializerMethodField()

    class Meta:
        model = Testimonial
        fields = [
            "uid",
            "customer_name",
            "location",
            "capacity_kw",
            "system_label",
            "installed_on",
            "installed_on_label",
            "quote_en",
            "quote_ml",
            "bill_before",
            "bill_after",
            "photo",
            "photo_url",
            "photo_src",
            "is_active",
            "sort_order",
            "show_on_website",
            "created_at",
            "updated_at",
            "version",
            "expected_version",
        ]
        read_only_fields = ["uid", "photo_src", "created_at", "updated_at", "version"]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_photo_src(self, row):
        return photo_src(row)


class CampaignSerializer(_Versioned):
    image = PublicImageField(required=False, allow_null=True)

    class Meta:
        model = Campaign
        fields = ["uid", "title", "body_en", "body_ml", "image", "starts_on", "ends_on", "is_active", "created_at", "updated_at", "version", "expected_version"]
        read_only_fields = ["uid", "created_at", "updated_at", "version"]


def photo_src(row: Testimonial) -> str | None:
    if row.photo_id and row.photo and row.photo.cdn_url:
        return row.photo.cdn_url
    return row.photo_url or None


class PublicTestimonialSerializer(serializers.ModelSerializer):
    """``GET /api/public/v1/testimonials/`` (replaces ``/bom/api/quotation-testimonials/``)."""

    name = serializers.CharField(source="customer_name")
    quote = serializers.CharField(source="quote_en")
    photo_src = serializers.SerializerMethodField()
    monthly_saving = serializers.SerializerMethodField()

    class Meta:
        model = Testimonial
        fields = ["uid", "name", "location", "system_label", "capacity_kw", "installed_on", "quote", "quote_ml", "photo_src", "bill_before", "bill_after", "monthly_saving", "sort_order"]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_photo_src(self, row):
        return photo_src(row)

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True))
    def get_monthly_saving(self, row):
        if row.bill_before is None or row.bill_after is None:
            return None
        return str(max(row.bill_before - row.bill_after, 0))
