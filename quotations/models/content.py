"""Quotation content (PLAN §1.2 ContentRelease, §2.6): content versions, inclusions, tier display names, testimonials,
campaigns.

``quotations_content_version`` holds the bilingual document content (every text leaf ``{en, ml}``, the Flarize
``quotation-content.json`` shape the payload assembler reads) and its fit report. Publishing freezes, beside it, the
inclusion matrix, tier names, testimonials and the active campaign as they were at that moment (``release_payload``):
a PUBLISHED row is the ContentRelease every issued version pins.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from core.models import BaseModel
from quotations.models.choices import ContentStatus, InclusionKind, SystemType, Tier, in_choices

LIVE = Q(deleted_at__isnull=True)


class ContentVersion(BaseModel):
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=ContentStatus.choices, default=ContentStatus.DRAFT)
    language_payload = models.JSONField(help_text="Bilingual content (labels, terms, timeline, appliances … every text {en, ml}).")
    fit_report = models.JSONField(default=dict, blank=True, help_text="engines.content_fit.fit_summary of language_payload.")
    release_payload = models.JSONField(null=True, blank=True, help_text="Frozen at publish: inclusions, tier names, testimonials, campaign.")
    release_sha256 = models.CharField(max_length=64, blank=True, default="")
    note = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "quotations_content_version"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="quotations_content_version_number_uniq"),
            models.UniqueConstraint(fields=["status"], condition=LIVE & Q(status="DRAFT"), name="quotations_content_version_one_draft"),
            models.UniqueConstraint(fields=["status"], condition=Q(status="PUBLISHED"), name="quotations_content_version_one_published"),
            models.CheckConstraint(condition=in_choices("status", ContentStatus), name="quotations_content_version_status_valid"),
            models.CheckConstraint(condition=Q(number__gte=1), name="quotations_content_version_number_positive"),
            models.CheckConstraint(condition=Q(status="DRAFT") | Q(published_at__isnull=False, release_payload__isnull=False), name="quotations_content_version_published_is_frozen"),
        ]

    def __str__(self) -> str:
        return f"Content v{self.number} ({self.status})"


class Inclusion(BaseModel):
    key = models.CharField(max_length=64)
    kind = models.CharField(max_length=10, choices=InclusionKind.choices, default=InclusionKind.COMPONENT)
    label_en = models.CharField(max_length=160)
    label_ml = models.CharField(max_length=200, blank=True, default="")
    default_on = models.BooleanField(default=True)
    applies_to = models.JSONField(default=dict, help_text='Per tier: {"BASE": true|false|null|"text", "VALUE": …, "PREMIUM": …}.')
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "quotations_inclusion"
        ordering = ["kind", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["key"], condition=LIVE, name="quotations_inclusion_key_live_uniq"),
            models.CheckConstraint(condition=in_choices("kind", InclusionKind), name="quotations_inclusion_kind_valid"),
            models.CheckConstraint(condition=Q(key__regex=r"^[A-Za-z][A-Za-z0-9_]{0,63}$"), name="quotations_inclusion_key_format"),
        ]

    def __str__(self) -> str:
        return self.key


class TierDisplayName(BaseModel):
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    name_en = models.CharField(max_length=80)
    name_ml = models.CharField(max_length=120, blank=True, default="")
    is_recommended = models.BooleanField(default=False)
    badge_en = models.CharField(max_length=80, blank=True, default="")
    badge_ml = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        db_table = "quotations_tier_display_name"
        ordering = ["system_type", "tier"]
        constraints = [
            models.UniqueConstraint(fields=["system_type", "tier"], condition=LIVE, name="quotations_tier_display_name_uniq"),
            models.UniqueConstraint(fields=["system_type"], condition=LIVE & Q(is_recommended=True), name="quotations_tier_display_name_one_recommended"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="quotations_tier_display_name_system_type_valid"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="quotations_tier_display_name_tier_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.system_type}/{self.tier}: {self.name_en}"


class Testimonial(BaseModel):
    customer_name = models.CharField(max_length=80)
    location = models.CharField(max_length=80, blank=True, default="")
    capacity_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    system_label = models.CharField(max_length=40, blank=True, default="", help_text='Printed system label, e.g. "5 kW System".')
    installed_on = models.DateField(null=True, blank=True, help_text="Only month and year are printed.")
    installed_on_label = models.CharField(max_length=40, blank=True, default="", help_text="Free-text install date when no date is known (Flarize).")
    quote_en = models.TextField(max_length=1000)
    quote_ml = models.TextField(max_length=1000, blank=True, default="")
    bill_before = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="Monthly bill before solar (INR).")
    bill_after = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="Monthly bill after solar (INR).")
    # SET_NULL: the photo is a public media asset; deleting it is refused while referenced (media.usage).
    photo = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    photo_url = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    show_on_website = models.BooleanField(default=True)

    class Meta:
        db_table = "quotations_testimonial"
        ordering = ["sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=~Q(customer_name=""), name="quotations_testimonial_name_present"),
            models.CheckConstraint(condition=~Q(quote_en=""), name="quotations_testimonial_quote_present"),
            models.CheckConstraint(condition=Q(capacity_kw__isnull=True) | Q(capacity_kw__gt=0), name="quotations_testimonial_capacity_positive"),
            models.CheckConstraint(condition=Q(bill_before__isnull=True) | Q(bill_before__gte=0), name="quotations_testimonial_bill_before_positive"),
            models.CheckConstraint(condition=Q(bill_after__isnull=True) | Q(bill_after__gte=0), name="quotations_testimonial_bill_after_positive"),
            models.CheckConstraint(condition=Q(bill_before__isnull=True) | Q(bill_after__isnull=True) | Q(bill_after__lte=F("bill_before")), name="quotations_testimonial_bill_after_not_above_before"),
        ]
        indexes = [models.Index(fields=["show_on_website", "is_active", "sort_order"], name="quotations_testimonial_public")]

    def __str__(self) -> str:
        return f"{self.customer_name} ({self.location})"


class Campaign(BaseModel):
    title = models.CharField(max_length=150)
    body_en = models.TextField(blank=True, default="")
    body_ml = models.TextField(blank=True, default="")
    # SET_NULL: public media asset (page 2 artwork).
    image = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=False)

    class Meta:
        db_table = "quotations_campaign"
        ordering = ["-starts_on", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(title=""), name="quotations_campaign_title_present"),
            models.CheckConstraint(condition=Q(starts_on__isnull=True) | Q(ends_on__isnull=True) | Q(ends_on__gte=F("starts_on")), name="quotations_campaign_dates_ordered"),
        ]

    def __str__(self) -> str:
        return self.title
