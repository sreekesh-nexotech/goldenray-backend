"""Shapes of ``pricing/releases/`` (list, detail, preview report, publish, diff)."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from pricing.models import PriceRelease
from pricing.serializers.common import PricingUserRefSerializer


class ReleaseSetRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField()


class ReleaseSerializer(serializers.ModelSerializer):
    market_rate_set = ReleaseSetRefSerializer(read_only=True)
    published_by = PricingUserRefSerializer(read_only=True, allow_null=True)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = PriceRelease
        fields = ["uid", "number", "status", "published_at", "published_by", "superseded_at", "market_rate_set", "note", "payload_sha256", "summary"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField(child=serializers.IntegerField()))
    def get_summary(self, release):
        return (release.publish_report or {}).get("summary", {})


class ReleaseDetailSerializer(ReleaseSerializer):
    payload = serializers.SerializerMethodField(help_text="The immutable snapshot; landed costs and margin configuration only with pricing_internal.view.")
    publish_report = serializers.JSONField(read_only=True)

    class Meta(ReleaseSerializer.Meta):
        fields = [*ReleaseSerializer.Meta.fields, "payload", "publish_report"]
        read_only_fields = fields

    @extend_schema_field(serializers.JSONField())
    def get_payload(self, release):
        from pricing.services.releases import redact_payload

        return redact_payload(release.payload, internal=self.context.get("internal", False))


class ReportItemSerializer(serializers.Serializer):
    severity = serializers.ChoiceField(choices=[("BLOCK", "Blocks publishing"), ("WARN", "Warning"), ("INFO", "Information")])
    code = serializers.CharField()
    message = serializers.CharField()
    context = serializers.DictField()


class ChangeCountsSerializer(serializers.Serializer):
    added = serializers.IntegerField()
    removed = serializers.IntegerField()
    changed = serializers.IntegerField()


class PublishReportSerializer(serializers.Serializer):
    can_publish = serializers.BooleanField()
    counts = serializers.DictField(child=serializers.IntegerField(), help_text="{BLOCK, WARN, INFO}")
    items = ReportItemSerializer(many=True)
    summary = serializers.DictField(child=serializers.IntegerField())
    payload_sha256 = serializers.CharField(allow_null=True)
    next_number = serializers.IntegerField()
    current_number = serializers.IntegerField(allow_null=True)
    changes = serializers.DictField(child=ChangeCountsSerializer(), help_text="Per payload section, against the current release.")


class PublishSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_current_number = serializers.IntegerField(
        min_value=0, required=False, help_text="The current release number the publisher previewed (0: none); 409 `stale_version` when another release was published."
    )


class ChangedFieldSerializer(serializers.Serializer):
    key = serializers.CharField()
    field = serializers.CharField(allow_blank=True)
    old = serializers.JSONField(allow_null=True)
    new = serializers.JSONField(allow_null=True)


class SectionDiffSerializer(serializers.Serializer):
    added = serializers.ListField(child=serializers.CharField())
    removed = serializers.ListField(child=serializers.CharField())
    changed = ChangedFieldSerializer(many=True)


class ReleaseDiffSerializer(serializers.Serializer):
    current = serializers.IntegerField()
    against = serializers.IntegerField(allow_null=True)
    sections = serializers.DictField(child=SectionDiffSerializer())
