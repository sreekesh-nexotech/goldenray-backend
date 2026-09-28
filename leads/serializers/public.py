"""Website (public) shapes: OTP, lead, affiliate application, warranty request, installations.

Every field of the legacy forms has a place here (the legacy shim renames, it does not drop): ``phone_number`` →
``phone``, ``source`` → ``form`` (legacy lower-case values accepted), ``page`` → ``page``, ``details`` → ``details``;
the affiliate/warranty forms keep their names; ``name`` on ``otp/send`` is accepted (it is not stored — the lead
carries the name). Phone numbers must be Indian mobile numbers, as before.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from customers.models.customer import PINCODE_RE
from customers.serializers.customers import PhoneField
from leads.models import AffiliateApplication, CustomerInstallation, IssueType, KeralaDistrict, Lead, Profession, WarrantyRequest
from leads.models.choices import by_label
from leads.serializers.fields import HoneypotMixin, LenientChoiceField
from leads.services.intake import FORM_KIND, MAX_CALCULATOR_BYTES, MAX_SYSTEM_DETAILS_BYTES, PHONE_REQUIRED_KINDS, PayloadError, build_payload, clean_details, clean_document, clean_utm

FORMS = by_label(Lead.Form)


class OtpSendSerializer(serializers.Serializer):
    phone = PhoneField(mobile_only=True, help_text="Indian mobile number in any common spelling.")
    name = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Accepted for legacy forms; not stored.")


class OtpSendResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="`pending` — a code was sent.")
    phone = serializers.CharField(help_text="The number in E.164.")
    expires_at = serializers.DateTimeField()


class OtpVerifySerializer(serializers.Serializer):
    phone = PhoneField(mobile_only=True)
    code = serializers.RegexField(r"^[0-9]{4,10}$", max_length=10, error_messages={"invalid": "Enter the code from the SMS."})
    name = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Accepted for legacy forms; not stored.")


class OtpVerifyResponseSerializer(serializers.Serializer):
    status = serializers.CharField(help_text="`approved`.")
    verification_token = serializers.CharField(help_text="Send it with `POST leads` for this phone number.")
    expires_at = serializers.DateTimeField()


class LeadSubmitSerializer(HoneypotMixin, serializers.Serializer):
    kind = serializers.ChoiceField(choices=Lead.Kind.choices, required=False, help_text="Derived from `form` when omitted.")
    form = LenientChoiceField(Lead.Form, required=False, help_text="The website form (legacy `source`; lower-case legacy values accepted).")
    name = serializers.CharField(max_length=255)
    phone = PhoneField(mobile_only=True, required=False, allow_blank=True, source="phone_e164", help_text="Required for every kind but CONTACT.")
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    pincode = serializers.RegexField(PINCODE_RE, max_length=6, required=False, allow_blank=True, error_messages={"invalid": "Enter a 6-digit pincode."})
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)
    message = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    page = serializers.CharField(max_length=255, required=False, allow_blank=True, source="source_url", help_text="Site path the form was submitted from.")
    details = serializers.JSONField(required=False, allow_null=True, help_text="Flat object of at most 30 text/number/boolean fields (legacy `details`).")
    calculator = serializers.JSONField(required=False, allow_null=True, help_text="Calculator inputs/outputs (object, ≤ 8 KB).")
    utm = serializers.JSONField(required=False, allow_null=True, help_text="utm_source, utm_medium, utm_campaign, utm_term, utm_content, gclid, fbclid, referrer.")
    verification_token = serializers.CharField(max_length=2048, required=False, allow_blank=True, write_only=True, help_text="From `otp/verify`; required with a phone number.")

    def _missing_contact(self, data) -> str | None:
        """The phone/e-mail requirement of the (raw) kind, reported together with the other field errors."""
        if not hasattr(data, "get") or str(data.get("phone") or "").strip():
            return None
        kind = str(data.get("kind") or "").upper()
        form = FORMS.get(str(data.get("form") or "").strip().casefold())
        if kind not in Lead.Kind.values:
            kind = FORM_KIND.get(form, Lead.Kind.CONTACT)
        if kind in PHONE_REQUIRED_KINDS:
            return "This field is required."
        return None if str(data.get("email") or "").strip() else "Give a phone number or an e-mail address."

    def to_internal_value(self, data):
        missing = self._missing_contact(data)
        try:
            value = super().to_internal_value(data)
        except serializers.ValidationError as exc:
            if missing and isinstance(exc.detail, dict):
                exc.detail.setdefault("phone", [missing])
            raise
        if missing:
            raise serializers.ValidationError({"phone": [missing]})
        return value

    def validate_form(self, value):
        if value == Lead.Form.STUDIO:
            raise serializers.ValidationError("Not a website form.")
        return value

    def validate_email(self, value):
        return value.strip().lower()

    @staticmethod
    def _clean(cleaner, value, **kwargs):
        try:
            return cleaner(value, **kwargs)
        except PayloadError as exc:
            raise serializers.ValidationError(str(exc)) from None

    def validate_details(self, value):
        return self._clean(clean_details, value)

    def validate_calculator(self, value):
        return self._clean(clean_document, value, max_bytes=MAX_CALCULATOR_BYTES)

    def validate_utm(self, value):
        return self._clean(clean_utm, value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["payload"] = build_payload(details=attrs.pop("details", None), calculator=attrs.pop("calculator", None), utm=attrs.pop("utm", None))
        return attrs


class LeadReceiptSerializer(serializers.ModelSerializer):
    message = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        fields = ["uid", "number", "kind", "form", "status", "created_at", "message"]
        read_only_fields = fields

    def get_message(self, lead) -> str:
        return "Thank you! We'll be in touch shortly."


class AffiliateSubmitSerializer(HoneypotMixin, serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    phone = PhoneField(mobile_only=True, source="phone_e164")
    email = serializers.EmailField(max_length=254)
    profession = LenientChoiceField(Profession, help_text="Code, or the legacy label (e.g. `Real Estate Agent`).")
    district = LenientChoiceField(KeralaDistrict)

    def validate_email(self, value):
        return value.strip().lower()


class AffiliateReceiptSerializer(serializers.ModelSerializer):
    message = serializers.SerializerMethodField()

    class Meta:
        model = AffiliateApplication
        fields = ["uid", "full_name", "profession", "district", "status", "created_at", "message"]
        read_only_fields = fields

    def get_message(self, application) -> str:
        return "Message sent!"


class WarrantySubmitSerializer(HoneypotMixin, serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    phone = PhoneField(mobile_only=True, source="phone_e164")
    issue_type = LenientChoiceField(IssueType, help_text="Code, or the legacy label (e.g. `Inverter Fault`).")
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    system_details = serializers.JSONField(required=False, allow_null=True, help_text="Optional facts about the system (object, ≤ 4 KB).")

    def validate_system_details(self, value):
        try:
            return clean_document(value, max_bytes=MAX_SYSTEM_DETAILS_BYTES, max_depth=3)
        except PayloadError as exc:
            raise serializers.ValidationError(str(exc)) from None


class WarrantyReceiptSerializer(serializers.ModelSerializer):
    message = serializers.SerializerMethodField()

    class Meta:
        model = WarrantyRequest
        fields = ["uid", "full_name", "issue_type", "status", "created_at", "message"]
        read_only_fields = fields

    def get_message(self, request) -> str:
        return "Service request received. Our team will contact you shortly."


class PhotoSerializer(serializers.Serializer):
    url = serializers.URLField()
    alternative_text = serializers.CharField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)


class PublicInstallationSerializer(serializers.ModelSerializer):
    photo = serializers.SerializerMethodField()

    class Meta:
        model = CustomerInstallation
        fields = ["uid", "pincode", "district", "capacity_kw", "system_type", "installed_on", "photo"]
        read_only_fields = fields

    @extend_schema_field(PhotoSerializer(allow_null=True))
    def get_photo(self, installation):
        asset = installation.photo
        if asset is None or not asset.is_public or not asset.cdn_url or asset.deleted_at is not None:
            return None
        return {"url": asset.cdn_url, "alternative_text": asset.alternative_text, "width": asset.width, "height": asset.height}


class InstallationQuerySerializer(serializers.Serializer):
    pincode = serializers.CharField(max_length=12, required=False, allow_blank=True)
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)


class InstallationStatsQuerySerializer(serializers.Serializer):
    pincode = serializers.CharField(max_length=64, trim_whitespace=False, error_messages={"required": "Pincode parameter is required", "blank": "Pincode parameter is required"})


class InstallationStatsSerializer(serializers.Serializer):
    pincode = serializers.CharField()
    district = serializers.CharField()
    pincode_installations = serializers.IntegerField()
    district_installations = serializers.IntegerField()
    current_year_installations = serializers.IntegerField()
    year = serializers.IntegerField()
