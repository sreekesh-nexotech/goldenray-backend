"""``leads_customer_installation`` (PLAN §2.6): installations for the public showcase map and the pincode stats.

Typed columns of the legacy ``customer_installations`` (customer name, phone, pincode, address, size, date, status)
plus ``district``, ``system_type``, ``is_showcase``, ``photo`` and ``assignee``. Only COMPLETED rows count in the
stats (as before); only COMPLETED **showcase** rows are listed publicly, and never with the customer's name, phone
or address.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel

E164_RE = r"^\+[1-9][0-9]{7,14}$"


class CustomerInstallation(BaseModel):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PLANNED = "PLANNED", "Planned"

    class SystemType(models.TextChoices):
        ON_GRID = "ON_GRID", "On-grid"
        OFF_GRID = "OFF_GRID", "Off-grid"
        HYBRID = "HYBRID", "Hybrid"

    customer_name = models.CharField(max_length=255)
    phone_e164 = models.CharField(max_length=16, blank=True, default="")
    pincode = models.CharField(max_length=6)
    district = models.CharField(max_length=100, blank=True, default="")
    address = models.TextField(blank=True, default="")
    capacity_kw = models.DecimalField(max_digits=8, decimal_places=3)
    system_type = models.CharField(max_length=10, choices=SystemType.choices, blank=True, default="")
    installed_on = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPLETED)
    is_showcase = models.BooleanField(default=False)
    # Public showcase photo: SET_NULL (media is only soft-deleted and the usage guard refuses deleting it while used).
    photo = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Who looks after it (``leads`` owned scope): SET_NULL (attribution).
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "leads_customer_installation"
        ordering = ["-installed_on", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["COMPLETED", "IN_PROGRESS", "PLANNED"]), name="leads_customer_installation_status_valid"),
            models.CheckConstraint(condition=Q(system_type__in=["", "ON_GRID", "OFF_GRID", "HYBRID"]), name="leads_customer_installation_system_type_valid"),
            models.CheckConstraint(condition=Q(phone_e164="") | Q(phone_e164__regex=E164_RE), name="leads_customer_installation_phone_e164_format"),
            models.CheckConstraint(condition=Q(capacity_kw__gt=0), name="leads_customer_installation_capacity_positive"),
            models.CheckConstraint(condition=~Q(pincode=""), name="leads_customer_installation_pincode_present"),
        ]
        indexes = [
            # Stats: completed installations per pincode / per district / per year.
            models.Index(fields=["pincode", "status"], name="leads_installation_pincode"),
            models.Index(fields=["district", "status"], name="leads_installation_district"),
            models.Index(fields=["installed_on"], name="leads_installation_date"),
            # Public showcase list.
            models.Index(fields=["pincode", "installed_on"], name="leads_installation_showcase", condition=Q(is_showcase=True, status="COMPLETED", deleted_at__isnull=True)),
        ]

    def __str__(self) -> str:
        return f"{self.customer_name} — {self.pincode} ({self.capacity_kw} kW)"
