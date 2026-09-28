"""Columns every reference list shares (PLAN §2.8: "typed as today with ``is_active`` and ``sort_order``")."""

from __future__ import annotations

from django.db import models

from core.models import BaseModel


class ReferenceRow(BaseModel):
    is_active = models.BooleanField(default=True, help_text="Inactive rows are hidden from the website.")
    sort_order = models.IntegerField(default=0, help_text="Display order on the website (ascending).")

    class Meta:
        abstract = True
