"""``leads_affiliate_application`` and ``leads_warranty_request`` (PLAN §2.6): the two website forms with their own
staff workflow. Typed columns as in the legacy tables (``affiliate_application``, ``warranty_service_request``) plus
``status`` and ``assignee``; the warranty request links the customer with the same phone number when there is one.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel, CIEmailField
from leads.models.choices import KeralaDistrict

E164_RE = r"^\+[1-9][0-9]{7,14}$"


class Profession(models.TextChoices):
    REAL_ESTATE_AGENT = "REAL_ESTATE_AGENT", "Real Estate Agent"
    BUILDER_DEVELOPER = "BUILDER_DEVELOPER", "Builder / Developer"
    INFLUENCER = "INFLUENCER", "Influencer / Content Creator"
    FINANCIAL_ADVISOR = "FINANCIAL_ADVISOR", "Financial Advisor"
    FREELANCER = "FREELANCER", "Freelancer / Gig Worker"
    SOCIETY_RWA = "SOCIETY_RWA", "Society / RWA Representative"
    OTHER = "OTHER", "Other"


class IssueType(models.TextChoices):
    LOW_GENERATION = "LOW_GENERATION", "Low Generation"
    INVERTER_FAULT = "INVERTER_FAULT", "Inverter Fault"
    PANEL_DAMAGE = "PANEL_DAMAGE", "Panel Damage"
    NET_METERING = "NET_METERING", "KSEB / Net Metering"
    WARRANTY_CLAIM = "WARRANTY_CLAIM", "Warranty Claim"
    OTHER = "OTHER", "Other"


class AffiliateApplication(BaseModel):
    """A referral-partner application (``/solar-referral-program``)."""

    Profession = Profession

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        CONTACTED = "CONTACTED", "Contacted"
        APPROVED = "APPROVED", "Approved (partner onboarded)"
        REJECTED = "REJECTED", "Rejected"

    full_name = models.CharField(max_length=255)
    phone_e164 = models.CharField(max_length=16)
    email = CIEmailField(max_length=254)
    profession = models.CharField(max_length=24, choices=Profession.choices)
    district = models.CharField(max_length=32, choices=KeralaDistrict.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    # Who follows it up (``leads`` owned scope): SET_NULL (attribution).
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "leads_affiliate_application"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(profession__in=Profession.values), name="leads_affiliate_application_profession_valid"),
            models.CheckConstraint(condition=Q(district__in=KeralaDistrict.values), name="leads_affiliate_application_district_valid"),
            models.CheckConstraint(condition=Q(status__in=["NEW", "CONTACTED", "APPROVED", "REJECTED"]), name="leads_affiliate_application_status_valid"),
            models.CheckConstraint(condition=Q(phone_e164__regex=E164_RE), name="leads_affiliate_application_phone_e164_format"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="leads_affiliate_status_at"),
            models.Index(fields=["phone_e164"], name="leads_affiliate_phone"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.get_profession_display()})"


class WarrantyRequest(BaseModel):
    """A warranty / service request (``/solar-warranty``)."""

    IssueType = IssueType

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"
        REJECTED = "REJECTED", "Rejected (not covered / spam)"

    full_name = models.CharField(max_length=255)
    phone_e164 = models.CharField(max_length=16)
    # The customer with this phone number, when known: SET_NULL (customers are only soft-deleted; merges re-point it).
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issue_type = models.CharField(max_length=16, choices=IssueType.choices)
    description = models.TextField(blank=True, default="")
    system_details = models.JSONField(default=dict, blank=True, help_text="Validated system facts (capacity, inverter, installed on …).")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    # Who handles it (``leads`` owned scope): SET_NULL (attribution).
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "leads_warranty_request"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(issue_type__in=IssueType.values), name="leads_warranty_request_issue_type_valid"),
            models.CheckConstraint(condition=Q(status__in=["NEW", "IN_PROGRESS", "RESOLVED", "CLOSED", "REJECTED"]), name="leads_warranty_request_status_valid"),
            models.CheckConstraint(condition=Q(phone_e164__regex=E164_RE), name="leads_warranty_request_phone_e164_format"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="leads_warranty_status_at"),
            models.Index(fields=["phone_e164"], name="leads_warranty_phone"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.get_issue_type_display()})"
