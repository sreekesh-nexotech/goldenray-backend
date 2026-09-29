"""``reference_pincode`` (PLAN §2.8) and ``reference_pincode_office`` (DV-45).

The legacy ``pincodes`` table holds one row per *post office*: 5,057 rows for 1,428 Kerala pincodes, with offices of
one pincode spread over two districts or postal divisions in a few cases. The PLAN's ``reference_pincode`` is one
row per pincode (``pincode`` unique; ``serviceable``, ``distance_km_from_office``), so the per-office columns
(``office_name``, ``district``, ``state``, ``region``, ``division``) live in the child table and nothing is lost.
``reference_pincode.district``/``state`` are the pincode's primary values: the office with the lowest legacy id —
exactly what the legacy ``Pincode.objects.filter(pincode=…).first()`` lookups returned.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel
from reference.models.base import ReferenceRow

PINCODE_REGEX = r"^[1-9][0-9]{5}$"


class Pincode(ReferenceRow):
    pincode = models.CharField(max_length=6)
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    serviceable = models.BooleanField(default=True, help_text="Whether Flarize installs in this pincode.")
    distance_km_from_office = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "reference_pincode"
        ordering = ["pincode"]
        indexes = [
            # District lookups (installation stats: every pincode of a district).
            models.Index(fields=["district"], name="reference_pincode_district"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["pincode"], condition=Q(deleted_at__isnull=True), name="reference_pincode_live_uniq"),
            models.CheckConstraint(condition=Q(pincode__regex=PINCODE_REGEX), name="reference_pincode_format"),
            models.CheckConstraint(condition=Q(distance_km_from_office__isnull=True) | Q(distance_km_from_office__gte=0), name="reference_pincode_distance_non_negative"),
        ]

    def __str__(self) -> str:
        return self.pincode


class PincodeOffice(BaseModel):
    """One post office of a pincode (the grain of the legacy ``pincodes`` table)."""

    # True child: offices belong to their pincode.
    pincode = models.ForeignKey(Pincode, on_delete=models.CASCADE, related_name="offices")
    office_name = models.CharField(max_length=100)
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    region = models.CharField(max_length=100, blank=True, default="")
    division = models.CharField(max_length=100, blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "reference_pincode_office"
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["pincode", "sort_order"], name="reference_pincode_office_order"),
            # District lookups across offices (a pincode may straddle two districts).
            models.Index(fields=["district"], name="reference_pincode_office_dist"),
        ]

    def __str__(self) -> str:
        return f"{self.office_name} ({self.pincode_id})"
