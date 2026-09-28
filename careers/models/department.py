"""``careers_department`` (PLAN §2.8): hiring departments the positions belong to."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import BaseModel

SLUG_REGEX = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class Department(BaseModel):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    description = models.TextField(blank=True, default="", help_text="Short description shown on the careers page.")
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "careers_department"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), condition=Q(deleted_at__isnull=True), name="careers_department_name_live_uniq"),
            models.UniqueConstraint(fields=["slug"], condition=Q(deleted_at__isnull=True), name="careers_department_slug_live_uniq"),
            models.CheckConstraint(condition=Q(slug__regex=SLUG_REGEX), name="careers_department_slug_format"),
        ]

    def __str__(self) -> str:
        return self.name
