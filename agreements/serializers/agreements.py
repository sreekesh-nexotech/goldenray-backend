"""Agreement shapes (staff). Uids only; integer ids never leave the service layer."""

from __future__ import annotations

from decimal import Decimal

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from agreements.models import Agreement, AgreementKind, AgreementLine, Language, Phase, SourceType, SystemType, Variant
from core.serializers import ExpectedVersionMixin
from customers.serializers.customers import UserRefSerializer

BLANK_KINDS = [(AgreementKind.SALE_ORDER.value, AgreementKind.SALE_ORDER.label), (AgreementKind.EXTRA_STRUCTURE.value, AgreementKind.EXTRA_STRUCTURE.label)]
FROM_QUOTATION_KINDS = [(AgreementKind.PURCHASE_AGREEMENT.value, AgreementKind.PURCHASE_AGREEMENT.label), (AgreementKind.SALE_ORDER.value, AgreementKind.SALE_ORDER.label)]
MONEY = {"max_digits": 14, "decimal_places": 2, "min_value": 0}


class AgreementCustomerRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class AgreementRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    number = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    revision = serializers.IntegerField(read_only=True)


class AgreementComponentRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    sku = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class AgreementQuotationRefSerializer(serializers.Serializer):
    quotation_uid = serializers.UUIDField(source="quotation.uid", read_only=True)
    quotation_number = serializers.CharField(source="quotation.number", read_only=True)
    version_uid = serializers.UUIDField(source="uid", read_only=True)
    version = serializers.IntegerField(source="number", read_only=True)


class AgreementLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgreementLine
        fields = ["uid", "description", "quantity", "unit", "unit_price", "amount", "additional_work_item_uid", "sort_order"]
        read_only_fields = fields


class AgreementSerializer(serializers.ModelSerializer):
    customer = AgreementCustomerRefSerializer(read_only=True)
    owner = UserRefSerializer(read_only=True, allow_null=True)
    quotation = AgreementQuotationRefSerializer(source="quotation_version", read_only=True, allow_null=True)

    class Meta:
        model = Agreement
        fields = [
            "uid",
            "number",
            "kind",
            "status",
            "revision",
            "customer",
            "owner",
            "quotation",
            "language",
            "system_type",
            "capacity_kw",
            "phase",
            "variant",
            "final_price",
            "issued_at",
            "accepted_at",
            "cancelled_at",
            "legacy",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class AgreementDetailSerializer(AgreementSerializer):
    supersedes = AgreementRefSerializer(read_only=True, allow_null=True)
    issued_by = UserRefSerializer(read_only=True, allow_null=True)
    panel = AgreementComponentRefSerializer(read_only=True, allow_null=True)
    inverter = AgreementComponentRefSerializer(read_only=True, allow_null=True)
    battery = AgreementComponentRefSerializer(read_only=True, allow_null=True)
    structure_template_uid = serializers.UUIDField(source="structure_template.uid", read_only=True, allow_null=True)
    statutory_fee_uid = serializers.UUIDField(source="statutory_fee.uid", read_only=True, allow_null=True)
    registered_phone = serializers.CharField(source="registered_phone_e164", read_only=True)
    lines = AgreementLineSerializer(many=True, read_only=True)
    payload = serializers.SerializerMethodField()
    acceptance_scan = serializers.SerializerMethodField()

    class Meta(AgreementSerializer.Meta):
        fields = [
            *AgreementSerializer.Meta.fields,
            "source_type",
            "source_uid",
            "supersedes",
            "size_label",
            "panel",
            "panel_label",
            "panel_capacity_w",
            "panel_capacity_label",
            "panel_dcr",
            "panel_qty",
            "inverter",
            "inverter_brand",
            "inverter_type",
            "inverter_qty",
            "battery",
            "battery_label",
            "battery_qty",
            "structure_template_uid",
            "structure_type",
            "structure_material",
            "extra_structure",
            "walkway_required",
            "ladder_required",
            "original_price",
            "extra_cost",
            "discount",
            "statutory_fee_uid",
            "statutory_fee_label",
            "statutory_fee_amount",
            "add_on_offer",
            "extra_description",
            "price_override_reason",
            "consumer_number",
            "registered_phone",
            "wheeling_required",
            "lines",
            "payload_sha256",
            "payload",
            "issued_by",
            "accepted_via",
            "acceptance_note",
            "acceptance_scan",
            "superseded_at",
            "cancel_reason",
            "legacy_ref",
            "legacy_quotation_ref",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_payload(self, agreement):
        return agreement.payload

    @extend_schema_field(
        {"type": "object", "nullable": True, "properties": {"uid": {"type": "string", "format": "uuid"}, "url": {"type": "string"}, "expires_at": {"type": "string", "format": "date-time"}}}
    )
    def get_acceptance_scan(self, agreement):
        from media.services import signing

        asset = agreement.acceptance_asset
        if asset is None:
            return None
        request = self.context.get("request")
        version = getattr(request, "version", None) or "v1"
        link = signing.signed_url(asset, version=version, absolute=request.build_absolute_uri if request is not None else None)
        return {"uid": str(asset.uid), "url": link.url, "expires_at": link.expires_at}


class FromQuotationSerializer(serializers.Serializer):
    quotation_version_uid = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=FROM_QUOTATION_KINDS, default=AgreementKind.PURCHASE_AGREEMENT)
    language = serializers.ChoiceField(choices=Language.choices, required=False)


class AgreementLineInputSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=255)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    unit = serializers.CharField(max_length=12, required=False, allow_blank=True, default="")
    unit_price = serializers.DecimalField(**MONEY)
    additional_work_item_uid = serializers.UUIDField(required=False, allow_null=True)


class AgreementFieldsSerializer(serializers.Serializer):
    """Editable columns (a quotation-derived DRAFT accepts only the non-pinned ones: 400 ``field_pinned``)."""

    language = serializers.ChoiceField(choices=Language.choices, required=False)
    system_type = serializers.ChoiceField(choices=SystemType.choices, required=False)
    capacity_kw = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    size_label = serializers.CharField(max_length=120, required=False, allow_blank=True)
    phase = serializers.ChoiceField(choices=Phase.choices, required=False, allow_blank=True)
    variant = serializers.ChoiceField(choices=Variant.choices, required=False, allow_blank=True)
    panel_uid = serializers.UUIDField(required=False, allow_null=True)
    panel_qty = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    panel_capacity_label = serializers.CharField(max_length=64, required=False, allow_blank=True)
    inverter_uid = serializers.UUIDField(required=False, allow_null=True)
    inverter_qty = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    battery_uid = serializers.UUIDField(required=False, allow_null=True)
    battery_qty = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    structure_template_uid = serializers.UUIDField(required=False, allow_null=True)
    structure_type = serializers.CharField(max_length=24, required=False, allow_blank=True)
    structure_material = serializers.CharField(max_length=120, required=False, allow_blank=True)
    extra_structure = serializers.BooleanField(required=False)
    walkway_required = serializers.BooleanField(required=False)
    ladder_required = serializers.BooleanField(required=False)
    original_price = serializers.DecimalField(**MONEY, required=False, allow_null=True)
    extra_cost = serializers.DecimalField(**MONEY, required=False, allow_null=True)
    add_on_offer = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    extra_description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    consumer_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    registered_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    wheeling_required = serializers.BooleanField(required=False, allow_null=True)
    lines = AgreementLineInputSerializer(many=True, required=False, max_length=100)


class BlankAgreementSerializer(AgreementFieldsSerializer):
    kind = serializers.ChoiceField(choices=BLANK_KINDS)
    customer_uid = serializers.UUIDField()
    source_type = serializers.ChoiceField(choices=SourceType.choices, required=False, allow_blank=True)
    source_uid = serializers.UUIDField(required=False, allow_null=True)
    base_agreement_uid = serializers.UUIDField(required=False, allow_null=True, help_text="Copy the plant and equipment (and the base amount) from another agreement of the customer.")


class AgreementUpdateSerializer(ExpectedVersionMixin, AgreementFieldsSerializer):
    pass


class AgreementTransitionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass


class SupersedeSerializer(ExpectedVersionMixin, serializers.Serializer):
    language = serializers.ChoiceField(choices=Language.choices, required=False)
    quotation_version_uid = serializers.UUIDField(required=False, help_text="Re-pin the revision to another ISSUED version of the customer's quotations.")


class AgreementCancelSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class AcceptanceSerializer(ExpectedVersionMixin, serializers.Serializer):
    file = serializers.FileField(help_text="Scan of the paper-signed agreement (PDF, JPEG, PNG, WebP or HEIC).")
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class PriceOverrideSerializer(ExpectedVersionMixin, serializers.Serializer):
    discount = serializers.DecimalField(**MONEY, required=False)
    final_price = serializers.DecimalField(**MONEY, required=False)
    reason = serializers.CharField(max_length=2000)


class AgreementRenderSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=Language.choices)


class AgreementRenderJobSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    status = serializers.CharField()
    language = serializers.CharField()
    payload_sha256 = serializers.CharField()


class AgreementDocumentLinkSerializer(serializers.Serializer):
    url = serializers.CharField()
    expires_at = serializers.DateTimeField()
    language = serializers.CharField()
