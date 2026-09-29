"""Quotation shapes (staff). Internal cost and margin (``gross_margin_pct``, commercial snapshot cost domains, the
payload's ``pricing.internal``) are present only for readers holding ``pricing_internal.view``."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.serializers.customers import UserRefSerializer
from quotations.models import (
    BomSnapshot,
    CommercialSnapshot,
    DiscountRequest,
    DiscountStatus,
    EmailLog,
    Language,
    Phase,
    Quotation,
    QuotationSource,
    QuotationStatus,
    RoofType,
    SubsidyType,
    SystemType,
    Tier,
    Version,
    VersionStatus,
)
from quotations.services.common import can_see_internal

BATTERY_CONFIG_CHOICES = [("", "none"), ("0", "0"), ("1", "1"), ("2", "2")]


def _internal(serializer) -> bool:
    request = serializer.context.get("request")
    return bool(request and can_see_internal(request.user))


class QuotationCustomerRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class VersionSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Version
        fields = ["uid", "number", "status", "system_type", "tier", "size_key", "phase", "language", "final_price", "issued_at", "legacy", "version"]
        read_only_fields = fields


class QuotationSerializer(serializers.ModelSerializer):
    customer = QuotationCustomerRefSerializer(read_only=True)
    owner = UserRefSerializer(read_only=True, allow_null=True)
    current_version = VersionSummarySerializer(read_only=True, allow_null=True)

    class Meta:
        model = Quotation
        fields = [
            "uid",
            "number",
            "status",
            "customer",
            "owner",
            "current_version",
            "valid_until",
            "issued_at",
            "accepted_at",
            "cancelled_at",
            "expired_at",
            "lost_reason",
            "source",
            "district",
            "affiliate_ref",
            "legacy",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class QuotationDetailSerializer(QuotationSerializer):
    versions = serializers.SerializerMethodField()

    class Meta(QuotationSerializer.Meta):
        fields = [*QuotationSerializer.Meta.fields, "versions"]
        read_only_fields = fields

    @extend_schema_field(VersionSummarySerializer(many=True))
    def get_versions(self, quotation):
        return VersionSummarySerializer(sorted(quotation.versions.all(), key=lambda row: -row.number), many=True).data


class SelectionsSerializer(serializers.Serializer):
    tier_selections = serializers.DictField(child=serializers.DictField(child=serializers.CharField()), required=False, help_text="{base|value|premium: {category: component sku}}")
    offer_code = serializers.CharField(required=False, allow_blank=True, max_length=50)
    appliance_rows = serializers.ListField(child=serializers.DictField(), required=False, max_length=12, help_text="[{id, qty, hours}] (page 3)")
    validity_override_days = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=365, help_text="Needs quotations.approve.")


class VersionInputSerializer(serializers.Serializer):
    system_type = serializers.ChoiceField(choices=SystemType.choices)
    tier = serializers.ChoiceField(choices=Tier.choices)
    size_key = serializers.CharField(max_length=10)
    phase = serializers.ChoiceField(choices=Phase.choices, required=False, allow_null=True)
    battery_config = serializers.ChoiceField(choices=BATTERY_CONFIG_CHOICES, required=False, allow_blank=True)
    future_size_key = serializers.CharField(max_length=10, required=False, allow_blank=True)
    roof_type = serializers.ChoiceField(choices=RoofType.choices, required=False)
    distance_km = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False)
    vehicle_type = serializers.CharField(max_length=32, required=False, allow_blank=True)
    subsidy_type = serializers.ChoiceField(choices=SubsidyType.choices, required=False)
    ghs_houses = serializers.IntegerField(min_value=1, max_value=10000, required=False, allow_null=True)
    language = serializers.ChoiceField(choices=Language.choices, required=False)
    selections = SelectionsSerializer(required=False)


class QuotationCreateSerializer(VersionInputSerializer):
    customer_uid = serializers.UUIDField()
    source = serializers.ChoiceField(choices=QuotationSource.choices, required=False)
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)
    affiliate_ref = serializers.CharField(max_length=64, required=False, allow_blank=True)


class VersionUpdateSerializer(ExpectedVersionMixin, VersionInputSerializer):
    refresh_release = serializers.BooleanField(required=False, default=False, help_text="Re-pin the draft to the current PackRelease and ContentRelease.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("system_type", "tier", "size_key"):
            self.fields[name].required = False


class ReviseSerializer(VersionUpdateSerializer):
    pass


class QuotationTransitionSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class CancelSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class BomSnapshotSerializer(serializers.ModelSerializer):
    engineering_run_uid = serializers.UUIDField(source="engineering_run.uid", read_only=True, allow_null=True)

    class Meta:
        model = BomSnapshot
        fields = ["uid", "tier", "is_primary", "engineering_status", "engineering_run_uid", "lines", "lock_acknowledgements"]
        read_only_fields = fields


class CommercialSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommercialSnapshot
        fields = ["uid", "tier", "is_primary", "customer_total_incl_gst", "pins", "cost_lines", "margin_check"]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not _internal(self):
            cost_lines = dict(data.get("cost_lines") or {})
            cost_lines.pop("cost", None)
            data["cost_lines"] = cost_lines
            data["margin_check"] = None
            data["pins"] = {key: value for key, value in (data.get("pins") or {}).items() if key not in ("landedCostVersion", "marginVersion", "procurementPriceVersion")}
        return data


class EmailLogSerializer(serializers.ModelSerializer):
    sent_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = EmailLog
        fields = ["id", "channel", "to", "language", "status", "error", "created_at", "sent_at", "sent_by"]
        read_only_fields = fields


class VersionDetailSerializer(serializers.ModelSerializer):
    pack_release = serializers.IntegerField(source="pack_release.number", read_only=True, allow_null=True)
    price_release = serializers.IntegerField(source="price_release.number", read_only=True, allow_null=True)
    content_release = serializers.IntegerField(source="content_release.number", read_only=True, allow_null=True)
    issued_by = UserRefSerializer(read_only=True, allow_null=True)
    bom_snapshots = BomSnapshotSerializer(many=True, read_only=True)
    commercial_snapshots = CommercialSnapshotSerializer(many=True, read_only=True)
    email_logs = EmailLogSerializer(many=True, read_only=True)
    document = serializers.SerializerMethodField()
    document_status = serializers.SerializerMethodField()

    class Meta:
        model = Version
        fields = [
            "uid",
            "number",
            "status",
            "pack_release",
            "price_release",
            "content_release",
            "system_type",
            "tier",
            "size_key",
            "size_kw",
            "phase",
            "battery_config",
            "future_size_key",
            "structure_type",
            "roof_type",
            "distance_km",
            "vehicle_type",
            "subsidy_type",
            "ghs_houses",
            "language",
            "selections",
            "customer_price_incl_gst",
            "transport_extra",
            "offer_total",
            "discount_total",
            "final_price",
            "gross_margin_pct",
            "gate_report",
            "notices",
            "issued_at",
            "issued_by",
            "superseded_at",
            "document_payload_sha256",
            "document_status",
            "legacy",
            "bom_snapshots",
            "commercial_snapshots",
            "email_logs",
            "document",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_document(self, version):
        from quotations.services.quotations import payload_for_reader

        request = self.context.get("request")
        return payload_for_reader(version, request.user if request else None)

    @extend_schema_field(serializers.DictField(child=serializers.CharField(allow_null=True)))
    def get_document_status(self, version):
        return {"en": version.document_job.status if version.document_job_id else None, "ml": version.document_job_ml.status if version.document_job_ml_id else None}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not _internal(self):
            data["gross_margin_pct"] = None
        return data


class QuotationPreviewSerializer(serializers.Serializer):
    gate_report = serializers.JSONField()
    payload = serializers.JSONField()
    pricing = serializers.JSONField()
    at = serializers.CharField()


class SystemOptionsSerializer(serializers.Serializer):
    pack_release = serializers.IntegerField()
    options = serializers.JSONField(help_text="{ONGRID|HYBRID: {label, sizes[{size_key, label, phase, future_ready[]}], tiers, battery_configs, packs[]}}")
    roof_types = serializers.ListField(child=serializers.CharField())
    vehicles = serializers.JSONField()
    transport = serializers.JSONField()
    subsidy_types = serializers.ListField(child=serializers.CharField())
    tier_names = serializers.JSONField()


class DiscountRequestSerializer(serializers.ModelSerializer):
    version_number = serializers.IntegerField(source="quotation_version.number", read_only=True)
    requested_by = UserRefSerializer(read_only=True, allow_null=True)
    decided_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = DiscountRequest
        fields = ["uid", "version_number", "amount", "reason", "status", "requested_by", "decided_by", "decided_at", "note", "created_at", "version"]
        read_only_fields = fields


class DiscountCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    reason = serializers.CharField(max_length=2000)


class SendSerializer(serializers.Serializer):
    to = serializers.EmailField()
    channel = serializers.ChoiceField(choices=[("EMAIL", "E-mail"), ("WHATSAPP", "WhatsApp")], default="EMAIL")
    language = serializers.ChoiceField(choices=Language.choices, required=False)


class RenderSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=Language.choices)


class RenderJobRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    status = serializers.CharField()
    language = serializers.CharField()
    payload_sha256 = serializers.CharField()


class DocumentLinkSerializer(serializers.Serializer):
    url = serializers.CharField()
    expires_at = serializers.DateTimeField()
    language = serializers.CharField()


class AuditEventSerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    action = serializers.CharField()
    actor = serializers.SerializerMethodField()
    after = serializers.JSONField(allow_null=True)

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_actor(self, event):
        return str(event.actor.uid) if getattr(event, "actor", None) is not None else None


class HistorySerializer(serializers.Serializer):
    versions = VersionSummarySerializer(many=True)
    events = AuditEventSerializer(many=True)


STATUS_CHOICES = QuotationStatus.choices
VERSION_STATUS_CHOICES = VersionStatus.choices
DISCOUNT_STATUS_CHOICES = DiscountStatus.choices
