"""``customers_customer`` and ``customers_note`` (PLAN §2.6).

A customer is identified by its phone number: ``phone_e164`` is unique among live rows (partial unique index), so
leads, Flarize records and site-inspection imports all resolve to one row by phone — never by name. ``owner`` is the
record-scope anchor (Sales Executives see the customers they own). A merged customer points at the survivor
(``merged_into``) and is soft-deleted in the same transaction, which frees its phone number; the database refuses a
live row that claims to be merged.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from core.models import BaseModel, CIEmailField

E164_RE = r"^\+[1-9][0-9]{7,14}$"
PINCODE_RE = r"^[1-9][0-9]{5}$"


class Customer(BaseModel):
    class Source(models.TextChoices):
        WEBSITE = "WEBSITE", "Website"
        SALES_ENTRY = "SALES_ENTRY", "Sales entry"
        REFERRAL = "REFERRAL", "Referral"
        PA_IMPORT = "PA_IMPORT", "Purchase agreement import"
        SI_IMPORT = "SI_IMPORT", "Site inspection import"

    class BillCycle(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        BIMONTHLY = "BIMONTHLY", "Every two months"

    code = models.CharField(max_length=24, help_text="CUST-… (Flarize ids are kept for migrated rows).")
    name = models.CharField(max_length=255)
    phone_e164 = models.CharField(max_length=16, blank=True, default="", help_text="E.164; unique among live customers.")
    alt_phone = models.CharField(max_length=20, blank=True, default="", help_text="Second number as entered (E.164 when it parses).")
    email = CIEmailField(max_length=254, blank=True, default="")
    address = models.TextField(blank=True, default="")
    pincode = models.CharField(max_length=6, blank=True, default="")
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=120, blank=True, default="")
    google_map_link = models.URLField(max_length=500, blank=True, default="")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_bill = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Electricity bill amount (INR) per bill_cycle.")
    bill_cycle = models.CharField(max_length=12, choices=BillCycle.choices, blank=True, default="", help_text="The period current_bill covers (Flarize billCycle).")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SALES_ENTRY)
    # Record-scope anchor (``customers`` owned scope): SET_NULL, deleting a user never deletes customers.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The lead this customer was converted from: SET_NULL (attribution; the lead may be archived).
    lead = models.ForeignKey("leads.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Survivor of a merge: SET_NULL (the survivor is only ever soft-deleted, so the reference never dangles).
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "customers_customer"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["code"], condition=Q(deleted_at__isnull=True), name="customers_customer_code_live_uniq"),
            models.UniqueConstraint(fields=["phone_e164"], condition=Q(deleted_at__isnull=True) & ~Q(phone_e164=""), name="customers_customer_phone_live_uniq"),
            models.CheckConstraint(condition=Q(source__in=["WEBSITE", "SALES_ENTRY", "REFERRAL", "PA_IMPORT", "SI_IMPORT"]), name="customers_customer_source_valid"),
            models.CheckConstraint(condition=Q(bill_cycle__in=["", "MONTHLY", "BIMONTHLY"]), name="customers_customer_bill_cycle_valid"),
            models.CheckConstraint(condition=Q(phone_e164="") | Q(phone_e164__regex=E164_RE), name="customers_customer_phone_e164_format"),
            models.CheckConstraint(condition=Q(pincode="") | Q(pincode__regex=PINCODE_RE), name="customers_customer_pincode_format"),
            models.CheckConstraint(condition=~Q(code=""), name="customers_customer_code_present"),
            models.CheckConstraint(condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90), name="customers_customer_latitude_range"),
            models.CheckConstraint(condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180), name="customers_customer_longitude_range"),
            models.CheckConstraint(condition=Q(current_bill__isnull=True) | Q(current_bill__gte=0), name="customers_customer_current_bill_positive"),
            models.CheckConstraint(condition=Q(merged_into__isnull=True) | Q(deleted_at__isnull=False), name="customers_customer_merged_is_deleted"),
            models.CheckConstraint(condition=Q(merged_into__isnull=True) | ~Q(merged_into=F("id")), name="customers_customer_not_merged_into_self"),
        ]
        indexes = [
            # Name search (DRF SearchFilter → UPPER(name) LIKE UPPER('%…%')) uses this trigram index.
            GinIndex(OpClass(Upper("name"), name="gin_trgm_ops"), name="customers_customer_name_trgm"),
            models.Index(fields=["created_at"], name="customers_customer_created"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class CustomerNote(BaseModel):
    # A note belongs to its customer: CASCADE (true child; customers are only soft-deleted anyway).
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    pinned = models.BooleanField(default=False)

    class Meta:
        db_table = "customers_note"
        ordering = ["-pinned", "-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=~Q(body=""), name="customers_note_body_present")]
        indexes = [models.Index(fields=["customer", "created_at"], name="customers_note_customer_at")]

    def __str__(self) -> str:
        return f"Note on {self.customer_id}"
