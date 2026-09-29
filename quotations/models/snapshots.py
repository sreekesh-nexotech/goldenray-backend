"""``quotations_bom_snapshot`` and ``quotations_commercial_snapshot`` (PLAN §2.6; DV: one per tier).

A version quotes one primary pack and up to two alternative tiers (the three-option page), each priced from its own
LOCKED BOM snapshot and ISSUED commercial snapshot — so a snapshot row belongs to (version, tier) rather than to the
version alone. ``record`` keeps the engine document exactly as it was frozen (the lock record, the commercial
snapshot); the typed/JSON columns the PLAN names are projections of it for querying and reports.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel
from quotations.models.choices import Tier, in_choices


class BomSnapshot(BaseModel):
    # CASCADE: a snapshot is a true child of its version.
    quotation_version = models.ForeignKey("quotations.Version", on_delete=models.CASCADE, db_column="version_id", related_name="bom_snapshots")
    tier = models.CharField(max_length=8, choices=Tier.choices)
    is_primary = models.BooleanField(default=True)
    lines = models.JSONField(default=list)
    lock_acknowledgements = models.JSONField(default=list, blank=True)
    # SET_NULL: the engineering run recorded for this snapshot (QUOTATION_DRAFT); runs are never deleted anyway.
    engineering_run = models.ForeignKey("engineering.Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    engineering_status = models.CharField(max_length=8, blank=True, default="", help_text="VALID / WARNING / BLOCKED (checker verdict at lock).")
    record = models.JSONField(help_text="The frozen LOCKED BOM snapshot (bomLock/bomSnapshot record).")
    legacy_ref = models.CharField(max_length=96, blank=True, default="", help_text="Flarize bomSnapshotId of an imported snapshot.")

    class Meta:
        db_table = "quotations_bom_snapshot"
        ordering = ["quotation_version_id", "-is_primary", "tier"]
        constraints = [
            models.UniqueConstraint(fields=["quotation_version", "tier"], name="quotations_bom_snapshot_version_tier_uniq"),
            models.UniqueConstraint(fields=["quotation_version"], condition=Q(is_primary=True), name="quotations_bom_snapshot_one_primary"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="quotations_bom_snapshot_tier_valid"),
            models.CheckConstraint(condition=Q(engineering_status__in=["", "VALID", "WARNING", "BLOCKED"]), name="quotations_bom_snapshot_engineering_status_valid"),
        ]

    def __str__(self) -> str:
        return f"BOM snapshot {self.quotation_version_id}/{self.tier}"


class CommercialSnapshot(BaseModel):
    # CASCADE: a snapshot is a true child of its version.
    quotation_version = models.ForeignKey("quotations.Version", on_delete=models.CASCADE, db_column="version_id", related_name="commercial_snapshots")
    tier = models.CharField(max_length=8, choices=Tier.choices)
    is_primary = models.BooleanField(default=True)
    cost_lines = models.JSONField(default=dict, help_text="cost / pricing / pack / offer domains of the snapshot (internal).")
    pins = models.JSONField(default=dict, help_text="The 13 pinned versions + the platform release numbers.")
    margin_check = models.JSONField(default=dict, blank=True, help_text="landedCostCheck + reference margin (internal).")
    customer_total_incl_gst = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    record = models.JSONField(help_text="The frozen ISSUED commercial snapshot.")
    legacy_ref = models.CharField(max_length=96, blank=True, default="", help_text="Flarize commercial snapshotId of an imported snapshot.")

    class Meta:
        db_table = "quotations_commercial_snapshot"
        ordering = ["quotation_version_id", "-is_primary", "tier"]
        constraints = [
            models.UniqueConstraint(fields=["quotation_version", "tier"], name="quotations_commercial_snapshot_version_tier_uniq"),
            models.UniqueConstraint(fields=["quotation_version"], condition=Q(is_primary=True), name="quotations_commercial_snapshot_one_primary"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="quotations_commercial_snapshot_tier_valid"),
        ]

    def __str__(self) -> str:
        return f"Commercial snapshot {self.quotation_version_id}/{self.tier}"
