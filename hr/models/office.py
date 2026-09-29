"""``hr_office`` (PLAN §2.9): a place people work from. Its timezone decides every "today" for its people (A10)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

from core.models import BaseModel

DEFAULT_TIMEZONE = "Asia/Kolkata"


class Office(BaseModel):
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    address = models.TextField(blank=True, default="")
    timezone = models.CharField(max_length=60, default=DEFAULT_TIMEZONE, help_text="IANA zone (validated against zoneinfo).")
    # Attribution-like optional link: SET_NULL — deleting a shift must not delete the office; the office then has
    # no default timing and its people without a shift of their own fall back to the engine defaults.
    default_shift = models.ForeignKey("hr.Shift", null=True, blank=True, on_delete=models.SET_NULL, related_name="default_for_offices")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "hr_office"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Upper("code"), condition=Q(deleted_at__isnull=True), name="hr_office_code_live_uniq"),
            models.CheckConstraint(condition=~Q(code=""), name="hr_office_code_not_blank"),
            models.CheckConstraint(condition=~Q(timezone=""), name="hr_office_timezone_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
