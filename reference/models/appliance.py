"""``reference_appliance`` (PLAN §2.8): the appliance master used for daily-usage rows.

Typed after Flarize's appliance master (``quotation-content.json`` → ``appliances.master``: ``id``, ``name{en,ml}``,
``icon``, ``watts``, ``defaultHours``, ``optional``). ``code`` keeps the Flarize id (``lights_fans``, ``ev_car``)
because quotation appliance rows reference it.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from reference.models.base import ReferenceRow

APPLIANCE_CODE_REGEX = r"^[a-z0-9_]{2,32}$"


class Appliance(ReferenceRow):
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=100)
    name_ml = models.CharField(max_length=100, blank=True, default="", help_text="Malayalam name (falls back to English).")
    icon = models.CharField(max_length=16, blank=True, default="", help_text="Emoji or short icon key.")
    watts = models.PositiveIntegerField()
    default_hours = models.DecimalField(max_digits=4, decimal_places=2, help_text="Default hours of use per day (0–24).")
    is_optional = models.BooleanField(default=False, help_text="Offered as an add-on row (water pump, EV charging …).")

    class Meta:
        db_table = "reference_appliance"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["code"], condition=Q(deleted_at__isnull=True), name="reference_appliance_code_live_uniq"),
            models.CheckConstraint(condition=Q(code__regex=APPLIANCE_CODE_REGEX), name="reference_appliance_code_format"),
            models.CheckConstraint(condition=Q(default_hours__gte=0) & Q(default_hours__lte=24), name="reference_appliance_hours_range"),
        ]

    def __str__(self) -> str:
        return self.name
