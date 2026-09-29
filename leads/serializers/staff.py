"""Staff shapes for ``leads/`` and its sub-resources (affiliate applications, warranty requests, installations)."""

from __future__ import annotations

from decimal import Decimal

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.models.customer import PINCODE_RE
from customers.serializers.customers import ActiveUserField, PhoneField, UserRefSerializer
from leads.models import AffiliateApplication, CustomerInstallation, IssueType, KeralaDistrict, Lead, LeadEvent, LeadNote, Profession, WarrantyRequest
from leads.models.lead import OPEN_STATUS_CHOICES
from leads.serializers.fields import LenientChoiceField
from leads.serializers.public import PublicInstallationSerializer
from leads.services.intake import MAX_SYSTEM_DETAILS_BYTES, PayloadError, clean_details, clean_document
from media.models import MediaAsset


class CustomerRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


# ── Leads ───────────────────────────────────────────────────────────────────────────────────────────────────────────
class LeadSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="phone_e164", read_only=True)
    assignee = UserRefSerializer(read_only=True, allow_null=True)
    customer = CustomerRefSerializer(read_only=True, allow_null=True)
    page = serializers.CharField(source="source_url", read_only=True)

    class Meta:
        model = Lead
        fields = [
            "uid",
            "number",
            "kind",
            "form",
            "status",
            "name",
            "phone",
            "email",
            "pincode",
            "district",
            "message",
            "payload",
            "page",
            "otp_verified_at",
            "assignee",
            "customer",
            "lost_reason",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class _LeadWriteSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=Lead.Kind.choices)
    name = serializers.CharField(max_length=255)
    phone = PhoneField(source="phone_e164", required=False, allow_blank=True)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    pincode = serializers.RegexField(PINCODE_RE, max_length=6, required=False, allow_blank=True, error_messages={"invalid": "Enter a 6-digit pincode."})
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)
    message = serializers.CharField(max_length=5000, required=False, allow_blank=True)

    def validate_email(self, value):
        return value.strip().lower()

    def to_representation(self, instance):
        return LeadSerializer(instance, context=self.context).data


class LeadCreateSerializer(_LeadWriteSerializer):
    details = serializers.JSONField(required=False, allow_null=True, help_text="Flat object of at most 30 fields.")
    assignee_uid = ActiveUserField(source="assignee", required=False, allow_null=True, help_text="Default: you. Someone else (or nobody) needs leads.manage.")

    def validate_details(self, value):
        try:
            return clean_details(value)
        except PayloadError as exc:
            raise serializers.ValidationError(str(exc)) from None

    def validate(self, attrs):
        if not attrs.get("phone_e164") and not attrs.get("email"):
            raise serializers.ValidationError({"phone": ["Give a phone number or an e-mail address."]})
        return attrs


class LeadUpdateSerializer(ExpectedVersionMixin, _LeadWriteSerializer):
    kind = serializers.ChoiceField(choices=Lead.Kind.choices, required=False)
    name = serializers.CharField(max_length=255, required=False)


class AssignSerializer(ExpectedVersionMixin, serializers.Serializer):
    assignee_uid = ActiveUserField(source="assignee", allow_null=True, help_text="null unassigns.")


class LeadStatusSerializer(ExpectedVersionMixin, serializers.Serializer):
    status = serializers.ChoiceField(choices=OPEN_STATUS_CHOICES)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class LeadLostSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class LeadSpamSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class LeadConvertSerializer(ExpectedVersionMixin, serializers.Serializer):
    customer_uid = serializers.UUIDField(required=False, allow_null=True, help_text="Link this existing customer instead of matching by phone.")


class LeadConvertResultSerializer(serializers.Serializer):
    lead = LeadSerializer()
    customer = CustomerRefSerializer(help_text="A reference only: the matched customer may be owned by someone else (GET customers/<uid>/ if you may see it).")
    customer_created = serializers.BooleanField()


class LeadNoteSerializer(serializers.ModelSerializer):
    author = UserRefSerializer(source="created_by", read_only=True, allow_null=True)

    class Meta:
        model = LeadNote
        fields = ["uid", "body", "author", "created_at"]
        read_only_fields = fields


class LeadNoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=10_000)


class LeadEventSerializer(serializers.ModelSerializer):
    by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = LeadEvent
        fields = ["at", "event", "by", "data"]
        read_only_fields = fields


# ── Affiliate applications / warranty requests ──────────────────────────────────────────────────────────────────────
class AffiliateSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="phone_e164", read_only=True)
    assignee = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = AffiliateApplication
        fields = ["uid", "full_name", "phone", "email", "profession", "district", "status", "assignee", "created_at", "updated_at", "version"]
        read_only_fields = fields


class AffiliateUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    full_name = serializers.CharField(max_length=255, required=False)
    phone = PhoneField(source="phone_e164", required=False)
    email = serializers.EmailField(max_length=254, required=False)
    profession = LenientChoiceField(Profession, required=False)
    district = LenientChoiceField(KeralaDistrict, required=False)

    def to_representation(self, instance):
        return AffiliateSerializer(instance, context=self.context).data


# A warranty request (the inbox row); ``Warranty`` is the catalog's product warranty block.
@extend_schema_serializer(component_name="WarrantyRequest")
class WarrantySerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="phone_e164", read_only=True)
    assignee = UserRefSerializer(read_only=True, allow_null=True)
    customer = CustomerRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = WarrantyRequest
        fields = ["uid", "full_name", "phone", "issue_type", "description", "system_details", "customer", "status", "assignee", "created_at", "updated_at", "version"]
        read_only_fields = fields


class WarrantyUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    full_name = serializers.CharField(max_length=255, required=False)
    phone = PhoneField(source="phone_e164", required=False)
    issue_type = LenientChoiceField(IssueType, required=False)
    description = serializers.CharField(max_length=10_000, required=False, allow_blank=True)
    system_details = serializers.JSONField(required=False, allow_null=True)
    customer_uid = serializers.UUIDField(required=False, allow_null=True, help_text="Link a customer you can see (null unlinks).")

    def validate_system_details(self, value):
        try:
            return clean_document(value, max_bytes=MAX_SYSTEM_DETAILS_BYTES, max_depth=3)
        except PayloadError as exc:
            raise serializers.ValidationError(str(exc)) from None

    def to_representation(self, instance):
        return WarrantySerializer(instance, context=self.context).data


class AffiliateTransitionSerializer(ExpectedVersionMixin, serializers.Serializer):
    status = serializers.ChoiceField(choices=AffiliateApplication.Status.choices)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class WarrantyTransitionSerializer(ExpectedVersionMixin, serializers.Serializer):
    status = serializers.ChoiceField(choices=WarrantyRequest.Status.choices)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


# ── Installations ───────────────────────────────────────────────────────────────────────────────────────────────────
class InstallationSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="phone_e164", read_only=True)
    assignee = UserRefSerializer(read_only=True, allow_null=True)
    photo = serializers.SerializerMethodField()

    class Meta:
        model = CustomerInstallation
        fields = [
            "uid",
            "customer_name",
            "phone",
            "pincode",
            "district",
            "address",
            "capacity_kw",
            "system_type",
            "installed_on",
            "status",
            "is_showcase",
            "photo",
            "assignee",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_photo(self, installation):
        data = PublicInstallationSerializer().get_photo(installation)
        return {**data, "uid": str(installation.photo.uid)} if data else None


class PublicImageField(serializers.SlugRelatedField):
    def __init__(self, **kwargs):
        super().__init__(slug_field="uid", queryset=MediaAsset.objects.filter(visibility=MediaAsset.Visibility.PUBLIC, kind__in=[MediaAsset.Kind.IMAGE, MediaAsset.Kind.PHOTO]), **kwargs)


class _InstallationWriteSerializer(serializers.Serializer):
    customer_name = serializers.CharField(max_length=255)
    phone = PhoneField(source="phone_e164", required=False, allow_blank=True)
    pincode = serializers.RegexField(PINCODE_RE, max_length=6, error_messages={"invalid": "Enter a 6-digit pincode."})
    district = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Default: the district of the pincode.")
    address = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    capacity_kw = serializers.DecimalField(max_digits=8, decimal_places=3, min_value=Decimal("0.001"))
    system_type = serializers.ChoiceField(choices=[("", "Not set"), *CustomerInstallation.SystemType.choices], required=False, allow_blank=True)
    installed_on = serializers.DateField()
    status = serializers.ChoiceField(choices=CustomerInstallation.Status.choices, required=False)
    is_showcase = serializers.BooleanField(required=False)
    photo_uid = PublicImageField(source="photo", required=False, allow_null=True, help_text="A public IMAGE/PHOTO media asset.")
    assignee_uid = ActiveUserField(source="assignee", required=False, allow_null=True)

    def to_representation(self, instance):
        return InstallationSerializer(instance, context=self.context).data


class InstallationCreateSerializer(_InstallationWriteSerializer):
    pass


class InstallationUpdateSerializer(ExpectedVersionMixin, _InstallationWriteSerializer):
    customer_name = serializers.CharField(max_length=255, required=False)
    pincode = serializers.RegexField(PINCODE_RE, max_length=6, required=False, error_messages={"invalid": "Enter a 6-digit pincode."})
    capacity_kw = serializers.DecimalField(max_digits=8, decimal_places=3, min_value=Decimal("0.001"), required=False)
    installed_on = serializers.DateField(required=False)
