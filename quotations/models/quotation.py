"""``quotations_quotation``, ``quotations_version``, ``quotations_discount_request`` (PLAN §2.6).

A quotation is one offer to one customer: a number (``GR-<n>``, taken from ``core.sequences`` ``QUO`` at the first
issue and kept by every revision), an owner (the record-scope anchor) and a lifecycle status. Its versions are the
drafts and issued documents: at most one DRAFT at a time; an ISSUED version is immutable — the deep-frozen
``document_payload`` and its SHA-256 are written once, at issue, and never rebuilt. Legacy (Flarize) versions carry
``legacy = true`` and no PackRelease (they predate releases, PLAN §7.4).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from quotations.models.choices import (
    DiscountStatus,
    Language,
    Phase,
    QuotationSource,
    QuotationStatus,
    RoofType,
    SubsidyType,
    SystemType,
    Tier,
    VersionStatus,
    in_choices,
)

LIVE = Q(deleted_at__isnull=True)
SHA256_RE = r"^[0-9a-f]{64}$"
SIZE_KEY_RE = r"^[0-9]+(\.[0-9]{1,2})?(sp|tp)?$"


class Quotation(BaseModel):
    number = models.CharField(
        max_length=48, blank=True, default="", help_text="GR-<n> (core.sequences QUO), taken at the first issue; blank while never issued. Imported Flarize numbers are kept as printed."
    )
    # PROTECT: a customer with quotations is merged, never deleted (customers.services.merge re-points this column).
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="quotations")
    # SET_NULL: record-scope anchor (quotations `owned`); deleting a user never deletes quotations.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=12, choices=QuotationStatus.choices, default=QuotationStatus.DRAFT)
    # SET_NULL: a pointer to the version the quotation currently shows (versions are only ever superseded).
    current_version = models.ForeignKey("quotations.Version", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    valid_until = models.DateField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True, help_text="First issue.")
    accepted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.TextField(blank=True, default="")
    source = models.CharField(max_length=10, choices=QuotationSource.choices, default=QuotationSource.DIRECT)
    district = models.CharField(max_length=100, blank=True, default="")
    affiliate_ref = models.CharField(max_length=64, blank=True, default="")
    legacy = models.BooleanField(default=False, help_text="Imported from Flarize (quotation-state.json).")
    legacy_ref = models.CharField(max_length=96, blank=True, default="", help_text="Flarize quotationId of an imported quotation.")

    class Meta:
        db_table = "quotations_quotation"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=LIVE & ~Q(number=""), name="quotations_quotation_number_live_uniq"),
            models.UniqueConstraint(fields=["legacy_ref"], condition=LIVE & ~Q(legacy_ref=""), name="quotations_quotation_legacy_ref_uniq"),
            models.CheckConstraint(condition=in_choices("status", QuotationStatus), name="quotations_quotation_status_valid"),
            models.CheckConstraint(condition=in_choices("source", QuotationSource), name="quotations_quotation_source_valid"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "CANCELLED"]) | ~Q(number=""), name="quotations_quotation_issued_has_number"),
            models.CheckConstraint(condition=Q(affiliate_ref="") | Q(source="AFFILIATE"), name="quotations_quotation_affiliate_only_for_affiliate"),
            models.CheckConstraint(condition=~Q(source="DISTRICT") | ~Q(district=""), name="quotations_quotation_district_required"),
            models.CheckConstraint(condition=Q(accepted_at__isnull=True) | Q(status="ACCEPTED"), name="quotations_quotation_accepted_at_only_accepted"),
            models.CheckConstraint(condition=Q(status="ACCEPTED", accepted_at__isnull=False) | ~Q(status="ACCEPTED"), name="quotations_quotation_accepted_has_at"),
        ]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="quotations_quotation_owner"),
            models.Index(fields=["customer", "-created_at"], name="quotations_quotation_customer"),
            models.Index(fields=["status", "valid_until"], name="quotations_quotation_expiry"),
        ]

    def __str__(self) -> str:
        return self.number or f"Quotation {self.uid}"


class Version(BaseModel):
    # CASCADE: a version is a true child of its quotation (quotations are only soft-deleted anyway).
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=VersionStatus.choices, default=VersionStatus.DRAFT)
    # PROTECT: the releases a version was priced from are immutable masters (NULL only for legacy versions).
    pack_release = models.ForeignKey("packs.PackRelease", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    price_release = models.ForeignKey("pricing.PriceRelease", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # PROTECT: the ContentRelease (a PUBLISHED quotations_content_version) printed in the document.
    content_release = models.ForeignKey("quotations.ContentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    size_key = models.CharField(max_length=10, help_text="Pack size key: 3, 5sp, 5tp, 8 …")
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    phase = models.CharField(max_length=2, choices=Phase.choices)
    battery_config = models.CharField(max_length=1, blank=True, default="", help_text="Hybrid battery quantity 0/1/2; blank for on-grid.")
    future_size_key = models.CharField(max_length=10, blank=True, default="", help_text="Future-ready (upgrade) target size key.")
    structure_type = models.CharField(max_length=24, blank=True, default="", help_text="Structure template of the roof type (flatRoof, sheetRoof, elevated).")
    roof_type = models.CharField(max_length=10, choices=RoofType.choices, default=RoofType.FLAT)
    distance_km = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    vehicle_type = models.CharField(max_length=32, blank=True, default="")
    subsidy_type = models.CharField(max_length=12, choices=SubsidyType.choices, default=SubsidyType.NONE)
    ghs_houses = models.PositiveSmallIntegerField(null=True, blank=True)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    selections = models.JSONField(default=dict, blank=True, help_text="Swaps per tier, offer code, appliance rows, recommended tier (validated).")
    customer_price_incl_gst = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    transport_extra = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    offer_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="Sum of APPROVED discount requests.")
    final_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    gross_margin_pct = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Internal (pricing_internal): fraction vs. the reference cost.")
    gate_report = models.JSONField(null=True, blank=True)
    notices = models.JSONField(default=list, blank=True, help_text="Warnings raised while the version was a draft (e.g. a newer PackRelease).")
    issued_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: the rendered PDFs (English and Malayalam); the frozen payload is the source of truth, a job can be re-run.
    document_job = models.ForeignKey("documents.RenderJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    document_job_ml = models.ForeignKey("documents.RenderJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    document_payload = models.JSONField(null=True, blank=True, help_text="The deep-frozen issued document (payload, alternatives, freeze, pins).")
    document_payload_sha256 = models.CharField(max_length=64, blank=True, default="")
    legacy = models.BooleanField(default=False)

    class Meta:
        db_table = "quotations_version"
        ordering = ["quotation_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["quotation", "number"], name="quotations_version_number_uniq"),
            models.UniqueConstraint(fields=["quotation"], condition=LIVE & Q(status="DRAFT"), name="quotations_version_one_draft"),
            models.CheckConstraint(condition=in_choices("status", VersionStatus), name="quotations_version_status_valid"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="quotations_version_system_type_valid"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="quotations_version_tier_valid"),
            models.CheckConstraint(condition=in_choices("phase", Phase), name="quotations_version_phase_valid"),
            models.CheckConstraint(condition=in_choices("roof_type", RoofType), name="quotations_version_roof_type_valid"),
            models.CheckConstraint(condition=in_choices("subsidy_type", SubsidyType), name="quotations_version_subsidy_type_valid"),
            models.CheckConstraint(condition=in_choices("language", Language), name="quotations_version_language_valid"),
            models.CheckConstraint(condition=Q(battery_config__in=["", "0", "1", "2"]), name="quotations_version_battery_config_valid"),
            models.CheckConstraint(condition=Q(size_key__regex=SIZE_KEY_RE), name="quotations_version_size_key_format"),
            models.CheckConstraint(condition=Q(future_size_key="") | Q(future_size_key__regex=SIZE_KEY_RE), name="quotations_version_future_size_key_format"),
            models.CheckConstraint(condition=Q(number__gte=1), name="quotations_version_number_positive"),
            models.CheckConstraint(condition=Q(size_kw__gt=0), name="quotations_version_size_positive"),
            models.CheckConstraint(condition=Q(distance_km__gte=0), name="quotations_version_distance_not_negative"),
            models.CheckConstraint(condition=Q(offer_total__gte=0) & Q(discount_total__gte=0), name="quotations_version_reductions_not_negative"),
            models.CheckConstraint(condition=Q(legacy=True) | Q(pack_release__isnull=False, price_release__isnull=False), name="quotations_version_releases_pinned"),
            models.CheckConstraint(
                condition=Q(status="DRAFT") | Q(document_payload__isnull=False, document_payload_sha256__regex=SHA256_RE, issued_at__isnull=False),
                name="quotations_version_issued_is_frozen",
            ),
            models.CheckConstraint(condition=~Q(status="DRAFT") | Q(document_payload__isnull=True, document_payload_sha256=""), name="quotations_version_draft_not_frozen"),
        ]
        indexes = [models.Index(fields=["status", "pack_release"], name="quotations_version_release")]

    def __str__(self) -> str:
        return f"{self.quotation_id} v{self.number} ({self.status})"


class DiscountRequest(BaseModel):
    # CASCADE: a request is a child of the draft version it asks a discount on.
    quotation_version = models.ForeignKey(Version, on_delete=models.CASCADE, db_column="version_id", related_name="discount_requests")
    # SET_NULL: attribution (the requester; deny_self_action uses it).
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=DiscountStatus.choices, default=DiscountStatus.PENDING)
    # SET_NULL: attribution (the approver/rejecter).
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "quotations_discount_request"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["quotation_version"], condition=LIVE & Q(status="PENDING"), name="quotations_discount_one_pending"),
            models.CheckConstraint(condition=in_choices("status", DiscountStatus), name="quotations_discount_status_valid"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="quotations_discount_amount_positive"),
            models.CheckConstraint(condition=~Q(reason=""), name="quotations_discount_reason_present"),
            models.CheckConstraint(condition=Q(status="PENDING", decided_at__isnull=True) | (~Q(status="PENDING") & Q(decided_at__isnull=False)), name="quotations_discount_decided_consistent"),
        ]

    def __str__(self) -> str:
        return f"Discount {self.amount} on {self.quotation_version_id} ({self.status})"
