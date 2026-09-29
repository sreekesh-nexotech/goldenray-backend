"""``inventory_location`` (PLAN §2.3, optional stock ledger behind ``INVENTORY_STOCK``): a place stock is kept.

``office`` is the PLAN's ``office_id S``: the HR office a store room belongs to, optional. The relation is declared by
app label (``"hr.Office"``) and inventory never imports hr (import-linter contract "product master ignores content
and HR"); services resolve an office uid through the relation's own model. hr only soft-deletes offices, so the
``SET_NULL`` action never fires in practice — a location whose office was soft-deleted reads as having no office.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,29}$"


class Location(BaseModel):
    code = models.CharField(max_length=30, help_text="Short code, unique among live locations (case-insensitive), e.g. HO-STORE.")
    name = models.CharField(max_length=150)
    # SET_NULL: the office is an optional grouping; losing it never deletes a store or its ledger.
    # related_name="+": hr.Office gains no reverse accessor (hr knows nothing about inventory).
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "inventory_location"
        ordering = ["code", "id"]
        constraints = [
            models.UniqueConstraint(Upper("code"), condition=LIVE, name="inventory_location_code_live_uniq"),
            models.CheckConstraint(condition=Q(code__regex=CODE_PATTERN), name="inventory_location_code_format"),
            models.CheckConstraint(condition=~Q(name=""), name="inventory_location_name_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
