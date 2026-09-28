"""``leads_lead``, ``leads_lead_note``, ``leads_lead_event`` (PLAN §2.6).

One row per enquiry from any website form (``kind`` is the business classification, ``form`` the form it came from —
the legacy ``lead_collection_home.source``) or entered in Studio. Status workflow::

    NEW ─▶ CONTACTED ─▶ QUALIFIED ─▶ CONVERTED (via convert/, links a customer)
      └──────┴────────────┴──▶ LOST (reason required) / SPAM ─▶ (reopen) NEW

``leads_lead_event`` is the append-only history the Studio timeline shows.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import BaseModel, CIEmailField

E164_RE = r"^\+[1-9][0-9]{7,14}$"
PINCODE_RE = r"^[1-9][0-9]{5}$"


class Lead(BaseModel):
    class Kind(models.TextChoices):
        HOME_ENQUIRY = "HOME_ENQUIRY", "Home enquiry"
        ADVANCED_CALC = "ADVANCED_CALC", "Advanced calculator quote"
        GROUP_PURCHASE = "GROUP_PURCHASE", "Group purchase"
        CONTACT = "CONTACT", "Contact"
        REFERRAL = "REFERRAL", "Referral"
        QUOTE_REQUEST = "QUOTE_REQUEST", "Quotation request"

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        CONTACTED = "CONTACTED", "Contacted"
        QUALIFIED = "QUALIFIED", "Qualified"
        CONVERTED = "CONVERTED", "Converted"
        LOST = "LOST", "Lost"
        SPAM = "SPAM", "Spam"

    class Form(models.TextChoices):
        """Where the enquiry was captured — the legacy ``source`` values (upper-cased) plus Studio entry."""

        FOOTER = "FOOTER", "Footer — Ready to go solar"
        HOME_BOOKING = "HOME_BOOKING", "Book a consultation form"
        CONTACT_PAGE = "CONTACT_PAGE", "Contact Us page"
        GROUP_PURCHASE = "GROUP_PURCHASE", "Group Purchase reservation"
        QUOTATION = "QUOTATION", "Quotation request (calculator)"
        QUOTE_REQUEST = "QUOTE_REQUEST", "Quote request (advanced calculator)"
        REFERRAL_PARTNER = "REFERRAL_PARTNER", "Referral partner application"
        WARRANTY_SERVICE = "WARRANTY_SERVICE", "Warranty service request"
        OTHER = "OTHER", "Website form"
        STUDIO = "STUDIO", "Entered in Studio"

    OPEN_STATUSES = (Status.NEW, Status.CONTACTED, Status.QUALIFIED)

    number = models.CharField(max_length=24, help_text="L-<n> (core_sequence_counter LEAD).")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    form = models.CharField(max_length=20, choices=Form.choices, default=Form.OTHER)
    name = models.CharField(max_length=255)
    phone_e164 = models.CharField(max_length=16, blank=True, default="")
    email = CIEmailField(max_length=254, blank=True, default="")
    pincode = models.CharField(max_length=6, blank=True, default="")
    district = models.CharField(max_length=100, blank=True, default="")
    message = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True, help_text="Validated form extras: details, calculator inputs/outputs, utm.")
    otp_verified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    # Record-scope anchor (``leads`` owned = assigned to me): SET_NULL, deleting a user never deletes leads.
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The customer the lead became: SET_NULL (customers are only soft-deleted; merges re-point this column).
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    lost_reason = models.TextField(blank=True, default="")
    source_url = models.CharField(max_length=500, blank=True, default="", help_text="Site path or URL the form was submitted from.")
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "leads_lead"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="leads_lead_number_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["HOME_ENQUIRY", "ADVANCED_CALC", "GROUP_PURCHASE", "CONTACT", "REFERRAL", "QUOTE_REQUEST"]), name="leads_lead_kind_valid"),
            models.CheckConstraint(condition=Q(status__in=["NEW", "CONTACTED", "QUALIFIED", "CONVERTED", "LOST", "SPAM"]), name="leads_lead_status_valid"),
            models.CheckConstraint(
                condition=Q(form__in=["FOOTER", "HOME_BOOKING", "CONTACT_PAGE", "GROUP_PURCHASE", "QUOTATION", "QUOTE_REQUEST", "REFERRAL_PARTNER", "WARRANTY_SERVICE", "OTHER", "STUDIO"]),
                name="leads_lead_form_valid",
            ),
            models.CheckConstraint(condition=Q(phone_e164="") | Q(phone_e164__regex=E164_RE), name="leads_lead_phone_e164_format"),
            models.CheckConstraint(condition=Q(pincode="") | Q(pincode__regex=PINCODE_RE), name="leads_lead_pincode_format"),
            models.CheckConstraint(condition=~Q(number=""), name="leads_lead_number_present"),
            models.CheckConstraint(condition=~Q(status="LOST") | ~Q(lost_reason=""), name="leads_lead_lost_has_reason"),
            models.CheckConstraint(condition=~Q(status="CONVERTED") | Q(customer__isnull=False), name="leads_lead_converted_has_customer"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="leads_lead_status_created"),
            models.Index(fields=["phone_e164", "created_at"], name="leads_lead_phone_created"),
            models.Index(fields=["created_at"], name="leads_lead_created"),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.name}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


# The statuses ``leads/<uid>/status/`` accepts (the OpenAPI enum of that body).
OPEN_STATUS_CHOICES = [(value, label) for value, label in Lead.Status.choices if value in Lead.OPEN_STATUSES]


class LeadNote(BaseModel):
    # A note belongs to its lead: CASCADE (true child; leads are only soft-deleted anyway).
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()

    class Meta:
        db_table = "leads_lead_note"
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=~Q(body=""), name="leads_lead_note_body_present")]
        indexes = [models.Index(fields=["lead", "created_at"], name="leads_lead_note_lead_at")]


class LeadEvent(models.Model):
    """Append-only lead history (no base)."""

    class Event(models.TextChoices):
        CREATED = "CREATED", "Created"
        IMPORTED = "IMPORTED", "Imported"
        UPDATED = "UPDATED", "Updated"
        ASSIGNED = "ASSIGNED", "Assigned"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        NOTE_ADDED = "NOTE_ADDED", "Note added"
        CONVERTED = "CONVERTED", "Converted"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.BigAutoField(primary_key=True)
    # History belongs to its lead: CASCADE (true child).
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    # Who did it: SET_NULL (attribution; NULL = the website visitor or the system).
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    event = models.CharField(max_length=20, choices=Event.choices)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "leads_lead_event"
        ordering = ["-at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(event__in=["CREATED", "IMPORTED", "UPDATED", "ASSIGNED", "STATUS_CHANGED", "NOTE_ADDED", "CONVERTED", "ARCHIVED"]),
                name="leads_lead_event_event_valid",
            )
        ]
        indexes = [models.Index(fields=["lead", "at"], name="leads_lead_event_lead_at")]

    def __str__(self) -> str:
        return f"{self.event} on lead {self.lead_id}"
