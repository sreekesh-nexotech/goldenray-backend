"""Company profile shapes: staff read/update and the public website payload."""

from __future__ import annotations

import re

from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from company.models import CompanyProfile
from company.services.profile import quotation_offer
from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer

PHONE = RegexValidator(r"^\+[1-9][0-9]{6,14}$", "Use the international E.164 format, e.g. +919876543210.")
GSTIN = RegexValidator(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", "Not a valid GSTIN.")
PAN = RegexValidator(r"^[A-Z]{5}[0-9]{4}[A-Z]$", "Not a valid PAN.")
CIN = RegexValidator(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$", "Not a valid CIN.")
COUNTRY = RegexValidator(r"^[A-Z]{2}$", "Use the ISO 3166-1 alpha-2 code, e.g. IN.")
SOCIAL_PLATFORMS = ("facebook", "instagram", "linkedin", "youtube", "x", "whatsapp", "pinterest", "threads", "google_business")
MAX_TRUST_STATS = 12
MAX_RECIPIENTS = 20


class TrustStatSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=60)
    value = serializers.CharField(max_length=40)
    icon = serializers.CharField(max_length=40, required=False, allow_blank=True)


def _asset_field(help_text: str):
    return serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), allow_null=True, required=False, help_text=help_text)


class _Upper(serializers.CharField):
    def to_internal_value(self, data):
        return super().to_internal_value(data).upper()


class CompanyProfileSerializer(serializers.ModelSerializer):
    """Staff representation (``company.view``)."""

    logo = MediaAssetRefSerializer(read_only=True)
    letterhead = MediaAssetRefSerializer(read_only=True)
    seal = MediaAssetRefSerializer(read_only=True)
    signature = MediaAssetRefSerializer(read_only=True)
    upi_qr = MediaAssetRefSerializer(read_only=True)
    default_og_image = MediaAssetRefSerializer(read_only=True)
    quotation_offer_image = MediaAssetRefSerializer(read_only=True)
    trust_stats = TrustStatSerializer(many=True, read_only=True)
    social = serializers.DictField(child=serializers.URLField(), read_only=True)
    uid = serializers.SerializerMethodField(help_text="Null until the first edit creates the profile.")
    updated_at = serializers.SerializerMethodField(help_text="Null until the first edit creates the profile.")
    blog_revalidate_secret_set = serializers.SerializerMethodField()
    quotation_offer_active = serializers.SerializerMethodField()
    quotation_offer_image_src = serializers.SerializerMethodField()

    class Meta:
        model = CompanyProfile
        fields = [
            "uid",
            "legal_name",
            "trade_name",
            "gstin",
            "pan",
            "cin",
            "email",
            "phone_e164",
            "website",
            "address_line",
            "address_locality",
            "address_region",
            "postal_code",
            "country_code",
            "logo",
            "letterhead",
            "seal",
            "signature",
            "upi_qr",
            "default_og_image",
            "trust_stats",
            "social",
            "default_meta_description",
            "lead_notification_emails",
            "application_notification_emails",
            "notify_on_new_lead",
            "notify_on_new_application",
            "careers_accepting_general_applications",
            "careers_intro",
            "blog_revalidate_url",
            "blog_revalidate_secret_set",
            "quotation_offer_enabled",
            "quotation_offer_active",
            "quotation_offer_title",
            "quotation_offer_description",
            "quotation_offer_details",
            "quotation_offer_title_ml",
            "quotation_offer_description_ml",
            "quotation_offer_details_ml",
            "quotation_offer_valid_from",
            "quotation_offer_valid_until",
            "quotation_offer_image",
            "quotation_offer_image_url",
            "quotation_offer_image_src",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_uid(self, profile) -> str | None:
        return None if profile.pk is None else str(profile.uid)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_updated_at(self, profile) -> str | None:
        return None if profile.pk is None else serializers.DateTimeField().to_representation(profile.updated_at)

    @extend_schema_field(serializers.BooleanField())
    def get_blog_revalidate_secret_set(self, profile) -> bool:
        return bool(profile.blog_revalidate_secret)

    @extend_schema_field(serializers.BooleanField())
    def get_quotation_offer_active(self, profile) -> bool:
        return quotation_offer(profile).active

    @extend_schema_field(serializers.URLField())
    def get_quotation_offer_image_src(self, profile) -> str:
        return quotation_offer(profile).image_src


class CompanyProfileUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    legal_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    trade_name = serializers.CharField(max_length=160, required=False, allow_blank=True)
    gstin = _Upper(max_length=15, required=False, allow_blank=True, validators=[GSTIN])
    pan = _Upper(max_length=10, required=False, allow_blank=True, validators=[PAN])
    cin = _Upper(max_length=21, required=False, allow_blank=True, validators=[CIN])
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    phone_e164 = serializers.CharField(max_length=16, required=False, allow_blank=True, validators=[PHONE])
    website = serializers.URLField(max_length=200, required=False, allow_blank=True)
    address_line = serializers.CharField(max_length=255, required=False, allow_blank=True)
    address_locality = serializers.CharField(max_length=120, required=False, allow_blank=True)
    address_region = serializers.CharField(max_length=120, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    country_code = _Upper(max_length=2, required=False, validators=[COUNTRY])
    logo = _asset_field("Public IMAGE uid (null clears).")
    letterhead = _asset_field("IMAGE uid (null clears).")
    seal = _asset_field("IMAGE or SIGNATURE uid (null clears).")
    signature = _asset_field("SIGNATURE uid (null clears).")
    upi_qr = _asset_field("IMAGE uid (null clears).")
    default_og_image = _asset_field("Public IMAGE uid (null clears).")
    trust_stats = TrustStatSerializer(many=True, required=False)
    social = serializers.DictField(child=serializers.URLField(max_length=300), required=False, help_text=f"{{platform: https URL}}; platforms: {', '.join(SOCIAL_PLATFORMS)}.")
    default_meta_description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    lead_notification_emails = serializers.ListField(child=serializers.EmailField(max_length=254), required=False, max_length=MAX_RECIPIENTS)
    application_notification_emails = serializers.ListField(child=serializers.EmailField(max_length=254), required=False, max_length=MAX_RECIPIENTS)
    notify_on_new_lead = serializers.BooleanField(required=False)
    notify_on_new_application = serializers.BooleanField(required=False)
    careers_accepting_general_applications = serializers.BooleanField(required=False)
    careers_intro = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    blog_revalidate_url = serializers.URLField(max_length=500, required=False, allow_blank=True)
    blog_revalidate_secret = serializers.CharField(max_length=256, required=False, allow_blank=True, allow_null=True, write_only=True, help_text="Write-only; empty or null clears it.")
    quotation_offer_enabled = serializers.BooleanField(required=False)
    quotation_offer_title = serializers.CharField(max_length=150, required=False, allow_blank=True)
    quotation_offer_description = serializers.CharField(max_length=300, required=False, allow_blank=True)
    quotation_offer_details = serializers.CharField(max_length=300, required=False, allow_blank=True)
    quotation_offer_title_ml = serializers.CharField(max_length=150, required=False, allow_blank=True)
    quotation_offer_description_ml = serializers.CharField(max_length=300, required=False, allow_blank=True)
    quotation_offer_details_ml = serializers.CharField(max_length=300, required=False, allow_blank=True)
    quotation_offer_valid_from = serializers.DateField(required=False, allow_null=True)
    quotation_offer_valid_until = serializers.DateField(required=False, allow_null=True)
    quotation_offer_image = _asset_field("Public IMAGE uid (null clears); wins over quotation_offer_image_url.")
    quotation_offer_image_url = serializers.URLField(max_length=500, required=False, allow_blank=True)

    def validate_trust_stats(self, value):
        if len(value) > MAX_TRUST_STATS:
            raise serializers.ValidationError(f"At most {MAX_TRUST_STATS} entries.")
        return [{key: item[key] for key in ("label", "value", "icon") if item.get(key)} for item in value]

    def validate_social(self, value):
        unknown = sorted(set(value) - set(SOCIAL_PLATFORMS))
        if unknown:
            raise serializers.ValidationError(f"Unknown platform(s): {', '.join(unknown)}.")
        insecure = sorted(name for name, url in value.items() if not re.match(r"^https://", url))
        if insecure:
            raise serializers.ValidationError(f"Use https:// URLs ({', '.join(insecure)}).")
        return value

    def validate_lead_notification_emails(self, value):
        return list(dict.fromkeys(address.lower() for address in value))

    def validate_application_notification_emails(self, value):
        return list(dict.fromkeys(address.lower() for address in value))

    def to_representation(self, instance):
        return CompanyProfileSerializer(instance, context=self.context).data


class PublicAssetSerializer(serializers.Serializer):
    url = serializers.URLField()
    alternative_text = serializers.CharField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)


class PublicAddressSerializer(serializers.Serializer):
    line = serializers.CharField()
    locality = serializers.CharField()
    region = serializers.CharField()
    postal_code = serializers.CharField()
    country_code = serializers.CharField()


class PublicCareersSerializer(serializers.Serializer):
    accepting_general_applications = serializers.BooleanField()
    intro = serializers.CharField()


class PublicOfferSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    active = serializers.BooleanField(help_text="Print the offer banner today.")
    title = serializers.CharField()
    description = serializers.CharField()
    details = serializers.CharField()
    title_ml = serializers.CharField()
    description_ml = serializers.CharField()
    details_ml = serializers.CharField()
    valid_from = serializers.DateField(allow_null=True)
    valid_until = serializers.DateField(allow_null=True)
    image_src = serializers.URLField()


class PublicQuotationSerializer(serializers.Serializer):
    offer = PublicOfferSerializer()


class PublicCompanySerializer(serializers.Serializer):
    """``GET /api/public/<version>/company/`` — public fields only (no tax ids, bank data, recipients or secrets)."""

    name = serializers.CharField()
    legal_name = serializers.CharField()
    trade_name = serializers.CharField()
    email = serializers.CharField()
    phone = serializers.CharField()
    website = serializers.CharField()
    address = PublicAddressSerializer()
    logo = PublicAssetSerializer(allow_null=True)
    default_og_image = PublicAssetSerializer(allow_null=True)
    default_meta_description = serializers.CharField()
    trust_stats = TrustStatSerializer(many=True)
    social = serializers.DictField(child=serializers.URLField())
    careers = PublicCareersSerializer()
    quotation = PublicQuotationSerializer()

    @staticmethod
    def _asset(asset):
        if asset is None or asset.deleted_at is not None or not asset.is_public or not asset.cdn_url:
            return None
        return {"url": asset.cdn_url, "alternative_text": asset.alternative_text, "width": asset.width, "height": asset.height}

    def to_representation(self, profile):
        offer = quotation_offer(profile)
        return {
            "name": profile.display_name,
            "legal_name": profile.legal_name,
            "trade_name": profile.trade_name,
            "email": profile.email,
            "phone": profile.phone_e164,
            "website": profile.website,
            "address": {
                "line": profile.address_line,
                "locality": profile.address_locality,
                "region": profile.address_region,
                "postal_code": profile.postal_code,
                "country_code": profile.country_code,
            },
            "logo": self._asset(profile.logo),
            "default_og_image": self._asset(profile.default_og_image),
            "default_meta_description": profile.default_meta_description,
            "trust_stats": list(profile.trust_stats or []),
            "social": dict(profile.social or {}),
            "careers": {"accepting_general_applications": profile.careers_accepting_general_applications, "intro": profile.careers_intro},
            "quotation": {
                "offer": {
                    "enabled": offer.enabled,
                    "active": offer.active,
                    "title": offer.title,
                    "description": offer.description,
                    "details": offer.details,
                    "title_ml": offer.title_ml,
                    "description_ml": offer.description_ml,
                    "details_ml": offer.details_ml,
                    "valid_from": offer.valid_from.isoformat() if offer.valid_from else None,
                    "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
                    "image_src": offer.image_src,
                }
            },
        }
