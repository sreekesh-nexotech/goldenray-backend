"""``procurement_supplier`` (PLAN §2.4). ``contact`` is free metadata (``{name, phone, email, …}``, strings only).

Column beyond the PLAN list (DV): ``reference`` — the supplier's own reference (Flarize ``supplier.reference``).
"""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
GSTIN_PATTERN = r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}$"


class Supplier(BaseModel):
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=15, blank=True, default="")
    contact = models.JSONField(default=dict, blank=True)
    address = models.TextField(blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "procurement_supplier"
        ordering = ["code", "id"]
        constraints = [
            models.UniqueConstraint(Lower("code"), condition=LIVE, name="procurement_supplier_code_live_uniq"),
            models.CheckConstraint(condition=Q(code__regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$"), name="procurement_supplier_code_format"),
            models.CheckConstraint(condition=Q(gstin="") | Q(gstin__regex=GSTIN_PATTERN), name="procurement_supplier_gstin_format"),
            models.CheckConstraint(condition=~Q(name=""), name="procurement_supplier_name_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"
