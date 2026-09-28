"""``company_profile`` (PLAN §2.8): the one company row — identity, contact, brand assets, website defaults,
notification recipients, careers settings, blog revalidation and the quotation display settings.

Every field of the legacy CMS ``siteconfig.SiteSettings`` and of the main backend's ``bom.QuotationSettings`` has a
typed column here (mapping: docs/decisions/foundation-f3.md; DV-11 for the columns that differ from PLAN §2.8).
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q

from core.models import BaseModel
from flarize.crypto import EncryptedTextField

DEFAULT_OFFER_TITLE = "Priority 10-Day Installation"
DEFAULT_OFFER_DESCRIPTION = "Fast-tracked scheduling and execution"
DEFAULT_OFFER_TITLE_ML = "10 ദിവസത്തിനുള്ളിൽ മുൻഗണനാ ഇൻസ്റ്റലേഷൻ"
DEFAULT_OFFER_DESCRIPTION_ML = "വേഗത്തിലുള്ള ഷെഡ്യൂളിംഗും ഇൻസ്റ്റലേഷനും"
DEFAULT_OFFER_IMAGE_URL = "https://golden-ray.b-cdn.net/icons/37.png"


def _asset_fk(help_text: str) -> models.ForeignKey:
    # SET_NULL: a brand asset is optional; media.usage refuses to delete an asset while this row references it.
    return models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", help_text=help_text)


class CompanyProfile(BaseModel):
    # ── Identity ────────────────────────────────────────────────────────────────────────────────────────────────
    legal_name = models.CharField(max_length=200, blank=True, default="")
    trade_name = models.CharField(max_length=160, blank=True, default="")  # SiteSettings.company_name
    gstin = models.CharField(max_length=15, blank=True, default="")
    pan = models.CharField(max_length=10, blank=True, default="")
    cin = models.CharField(max_length=21, blank=True, default="")
    # ── Contact (SiteSettings company/admin information) ───────────────────────────────────────────────────────
    email = models.EmailField(max_length=254, blank=True, default="")
    phone_e164 = models.CharField(max_length=16, blank=True, default="")
    website = models.URLField(max_length=200, blank=True, default="")
    address_line = models.CharField(max_length=255, blank=True, default="")
    address_locality = models.CharField(max_length=120, blank=True, default="")  # city
    address_region = models.CharField(max_length=120, blank=True, default="")  # state
    postal_code = models.CharField(max_length=20, blank=True, default="")
    country_code = models.CharField(max_length=2, default="IN")
    # ── Brand assets ────────────────────────────────────────────────────────────────────────────────────────────
    logo = _asset_fk("Public image: website header and documents.")
    letterhead = _asset_fk("Document letterhead image.")
    seal = _asset_fk("Company seal printed on documents.")
    signature = _asset_fk("Authorised signatory's signature (private).")
    upi_qr = _asset_fk("UPI QR code printed on documents.")
    default_og_image = _asset_fk("Public image used when a page has no Open Graph image (SiteSettings.default_og_image).")
    # ── Website ─────────────────────────────────────────────────────────────────────────────────────────────────
    trust_stats = models.JSONField(default=list, blank=True)  # validated: [{label, value, icon?}]
    social = models.JSONField(default=dict, blank=True)  # validated: {platform: https URL}
    default_meta_description = models.TextField(blank=True, default="")
    # ── Notification preferences (SiteSettings) ────────────────────────────────────────────────────────────────
    lead_notification_emails = ArrayField(models.EmailField(max_length=254), default=list, blank=True)
    application_notification_emails = ArrayField(models.EmailField(max_length=254), default=list, blank=True)
    notify_on_new_lead = models.BooleanField(default=True)
    notify_on_new_application = models.BooleanField(default=True)
    # ── Careers (SiteSettings) ─────────────────────────────────────────────────────────────────────────────────
    careers_accepting_general_applications = models.BooleanField(default=True)
    careers_intro = models.TextField(blank=True, default="")
    # ── Blog revalidation (Next.js /api/revalidate) ────────────────────────────────────────────────────────────
    blog_revalidate_url = models.URLField(max_length=500, blank=True, default="")
    blog_revalidate_secret = EncryptedTextField(null=True, blank=True)  # write-only in the API
    # ── Quotation display settings (bom.QuotationSettings) ─────────────────────────────────────────────────────
    quotation_offer_enabled = models.BooleanField(default=True)
    quotation_offer_title = models.CharField(max_length=150, blank=True, default=DEFAULT_OFFER_TITLE)
    quotation_offer_description = models.CharField(max_length=300, blank=True, default=DEFAULT_OFFER_DESCRIPTION)
    quotation_offer_details = models.CharField(max_length=300, blank=True, default="")
    quotation_offer_title_ml = models.CharField(max_length=150, blank=True, default=DEFAULT_OFFER_TITLE_ML)
    quotation_offer_description_ml = models.CharField(max_length=300, blank=True, default=DEFAULT_OFFER_DESCRIPTION_ML)
    quotation_offer_details_ml = models.CharField(max_length=300, blank=True, default="")
    quotation_offer_valid_from = models.DateField(null=True, blank=True)
    quotation_offer_valid_until = models.DateField(null=True, blank=True)  # blank: runs as long as the quotation is valid
    quotation_offer_image = _asset_fk("Public offer banner image (wins over quotation_offer_image_url).")
    quotation_offer_image_url = models.URLField(max_length=500, blank=True, default=DEFAULT_OFFER_IMAGE_URL)

    class Meta:
        db_table = "company_profile"
        constraints = [
            # Singleton: at most one live row (a unique index on a constant expression).
            models.UniqueConstraint(models.Value(1), condition=Q(deleted_at__isnull=True), name="company_profile_singleton"),
            models.CheckConstraint(condition=Q(country_code__regex=r"^[A-Z]{2}$"), name="company_profile_country_code_iso"),
            models.CheckConstraint(
                condition=Q(quotation_offer_valid_from__isnull=True) | Q(quotation_offer_valid_until__isnull=True) | Q(quotation_offer_valid_until__gte=F("quotation_offer_valid_from")),
                name="company_profile_offer_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return self.trade_name or self.legal_name or "Company profile"

    @property
    def display_name(self) -> str:
        return self.trade_name or self.legal_name
