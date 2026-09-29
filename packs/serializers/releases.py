"""Serializers of ``packs/releases/`` and ``packs/compare/`` (staff).

Landed cost, gross margin and the pack-pricing ``internal`` block are internal data: shown only with
``pricing_internal.view`` (PLAN §3.2), the serializer context carries ``internal``.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from packs.models import PackRelease, Phase, ReleasePack, ReleaseStatus, SystemType, Tier

INTERNAL_PACK_FIELDS = ("landed_cost_total", "gross_margin_pct")


class PackReleaseUserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)


class PackReleaseSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(choices=ReleaseStatus.choices, read_only=True)
    config_version_number = serializers.IntegerField(source="config_version.number", read_only=True)
    price_release_number = serializers.IntegerField(source="price_release.number", read_only=True)
    published_by = PackReleaseUserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = PackRelease
        fields = ["uid", "number", "status", "config_version_number", "price_release_number", "content_release_uid", "published_at", "published_by", "superseded_at", "note", "payload_sha256"]
        read_only_fields = fields


class PackReleaseDetailSerializer(PackReleaseSerializer):
    publish_report = serializers.SerializerMethodField()
    pack_count = serializers.SerializerMethodField()

    class Meta(PackReleaseSerializer.Meta):
        fields = [*PackReleaseSerializer.Meta.fields, "pack_count", "publish_report"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_publish_report(self, obj) -> dict:
        return obj.publish_report

    def get_pack_count(self, obj) -> int:
        return obj.packs.count()


class ReleasePackSerializer(serializers.ModelSerializer):
    system_type = serializers.ChoiceField(choices=SystemType.choices, read_only=True)
    tier = serializers.ChoiceField(choices=Tier.choices, read_only=True)
    phase = serializers.ChoiceField(choices=Phase.choices, read_only=True)
    landed_cost_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text="Needs pricing_internal.view (null otherwise).")
    gross_margin_pct = serializers.DecimalField(max_digits=10, decimal_places=4, read_only=True, allow_null=True, help_text="Fraction; needs pricing_internal.view (null otherwise).")
    bom = serializers.JSONField(read_only=True)
    pricing = serializers.SerializerMethodField()

    class Meta:
        model = ReleasePack
        fields = [
            "key",
            "display_name",
            "system_type",
            "tier",
            "size_key",
            "size_kw",
            "phase",
            "battery_config",
            "future_size_key",
            "future_size_kw",
            "market_rate_key",
            "profile_key",
            "customer_price_incl_gst",
            "customer_price_excl_gst",
            "gst_amount",
            "landed_cost_total",
            "gross_margin_pct",
            "engineering_result",
            "bom",
            "pricing",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_pricing(self, obj) -> dict:
        pricing = dict(obj.pricing or {})
        if not self.context.get("internal"):
            pricing.pop("internal", None)
        return pricing

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("internal"):
            for name in INTERNAL_PACK_FIELDS:
                data[name] = None
        return data


class PackPublishSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_current_number = serializers.IntegerField(required=False, min_value=0, help_text="The current release number the preview was made against (0 = none).")


class PackReportItemSerializer(serializers.Serializer):
    severity = serializers.ChoiceField(choices=["BLOCK", "WARN", "INFO"])
    code = serializers.CharField()
    message = serializers.CharField()
    context = serializers.DictField()


class PackMatrixRowSerializer(serializers.Serializer):
    key = serializers.CharField()
    display_name = serializers.CharField()
    market_rate_key = serializers.CharField()
    status = serializers.ChoiceField(choices=["READY", "EXCLUDED"])
    reasons = serializers.ListField(child=serializers.CharField())
    price = serializers.JSONField(allow_null=True)
    engineering = serializers.CharField(required=False)


class PackPublishReportSerializer(serializers.Serializer):
    can_publish = serializers.BooleanField()
    counts = serializers.DictField(child=serializers.IntegerField())
    items = PackReportItemSerializer(many=True)
    matrix = PackMatrixRowSerializer(many=True)
    summary = serializers.DictField()
    payload_sha256 = serializers.CharField(allow_null=True)
    current_number = serializers.IntegerField(allow_null=True)
    next_number = serializers.IntegerField()


class PackCompareLineSerializer(serializers.Serializer):
    category = serializers.CharField(allow_null=True)
    item = serializers.CharField(allow_null=True)
    qty_before = serializers.JSONField(allow_null=True)
    qty_after = serializers.JSONField(allow_null=True)
    unit_price_before = serializers.JSONField(allow_null=True)
    unit_price_after = serializers.JSONField(allow_null=True)


class PackCompareEntrySerializer(serializers.Serializer):
    key = serializers.CharField()
    display_name = serializers.CharField()
    price_before = serializers.DecimalField(max_digits=14, decimal_places=2)
    price_after = serializers.DecimalField(max_digits=14, decimal_places=2)
    lines = PackCompareLineSerializer(many=True)


class PackCompareSerializer(serializers.Serializer):
    a = serializers.IntegerField()
    b = serializers.IntegerField()
    added = serializers.ListField(child=serializers.CharField())
    removed = serializers.ListField(child=serializers.CharField())
    changed = PackCompareEntrySerializer(many=True)
