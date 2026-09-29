"""``packs_config_version`` + its typed mirror (``packs_config_pack``, ``packs_config_line``) and pins.

The version's ``config`` (validated by ``engines.pack_config.schema``) is the authoring document. The typed tables are
derived from it for querying: every pack the document enumerates (system type × size × tier × future-ready pair ×
battery configuration), with the BOM the pack BOM builder resolves for it. ``packs_config_pin`` (DV) holds the
per-pack component pins that replace Flarize's package registry: a pin names the component of one slot of one pack
(``source`` MANUAL lines), everything else comes from the template.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from packs.models.choices import CURRENT_STATUSES, OPEN_STATUSES, ConfigStatus, LineSource, Phase, SystemType, Tier, in_choices

LIVE = Q(deleted_at__isnull=True)
SIZE_KEY = r"^[0-9]+(\.[0-9]{1,2})?(sp|tp)?$"
PACK_KEY = r"^[a-z0-9][a-z0-9.-]{0,63}$"


class ConfigVersion(BaseModel):
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=ConfigStatus.choices, default=ConfigStatus.DRAFT)
    # SET_NULL: lineage only; a version survives the one it was copied from.
    based_on = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derived")
    config = models.JSONField(null=True, blank=True, help_text="flarize.pack-config/1 sections; NULL only for imported history versions (Flarize kept no copy).")
    change_log = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True, default="")
    # SET_NULL: attribution only (the four actor columns below).
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    superseded_at = models.DateTimeField(null=True, blank=True)
    legacy_actor = models.CharField(max_length=64, blank=True, default="", help_text="Imported: the Flarize user id that approved the version when no platform user maps to it.")

    class Meta:
        db_table = "packs_config_version"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="packs_config_version_number_uniq"),
            models.UniqueConstraint(models.Value(1), condition=Q(status__in=OPEN_STATUSES) & LIVE, name="packs_config_version_one_open_draft"),
            models.UniqueConstraint(models.Value(1), condition=Q(status__in=CURRENT_STATUSES) & LIVE, name="packs_config_version_one_current"),
            models.CheckConstraint(condition=in_choices("status", ConfigStatus), name="packs_config_version_status_valid"),
            models.CheckConstraint(condition=Q(number__gte=1), name="packs_config_version_number_positive"),
            models.CheckConstraint(condition=Q(config__isnull=False) | Q(status=ConfigStatus.SUPERSEDED), name="packs_config_version_config_required"),
            models.CheckConstraint(condition=~Q(status=ConfigStatus.REJECTED) | ~Q(rejection_reason=""), name="packs_config_version_rejection_reason"),
        ]
        indexes = [models.Index(fields=["status", "-number"], name="packs_config_version_status")]

    def __str__(self) -> str:
        return f"Pack config v{self.number} ({self.status})"


class ConfigPack(BaseModel):
    # CASCADE: a pack is a true child of its config version. (Named config_version: `version` is the optimistic lock.)
    config_version = models.ForeignKey(ConfigVersion, on_delete=models.CASCADE, related_name="packs", db_column="version_id")
    key = models.CharField(max_length=64, help_text="ongrid-value-3, ongrid-base-3-up5sp, hybrid-value-3-b2 …")
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    size_key = models.CharField(max_length=10)
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    phase = models.CharField(max_length=2, choices=Phase.choices)
    battery_config = models.CharField(max_length=4, blank=True, default="", help_text="'' on-grid; '0', '1', '2' hybrid.")
    future_size_key = models.CharField(max_length=10, blank=True, default="")
    future_size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    is_future_ready = models.BooleanField(default=False)
    # SET_NULL: a future-ready pack points at the standard pack of its panel size.
    pair_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="future_ready")
    # PROTECT (panel, inverter, battery): components a pack resolves to are catalog masters.
    panel = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    inverter = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    battery = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    battery_qty = models.PositiveSmallIntegerField(default=0)
    # PROTECT: the structure template the base (flat roof) price assumes.
    structure_template = models.ForeignKey("bom.StructureTemplate", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    profile_key = models.CharField(max_length=32, blank=True, default="")
    market_rate_key = models.CharField(max_length=48, blank=True, default="")
    display_name = models.CharField(max_length=120, blank=True, default="")
    build_error = models.CharField(max_length=255, blank=True, default="", help_text="Why the BOM builder refused this pack (blank = built).")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "packs_config_pack"
        ordering = ["config_version_id", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["config_version", "key"], condition=LIVE, name="packs_config_pack_key_uniq"),
            models.UniqueConstraint(fields=["config_version", "system_type", "tier", "size_key", "phase", "battery_config", "future_size_key"], condition=LIVE, name="packs_config_pack_natural_uniq"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="packs_config_pack_system_type_valid"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="packs_config_pack_tier_valid"),
            models.CheckConstraint(condition=in_choices("phase", Phase), name="packs_config_pack_phase_valid"),
            models.CheckConstraint(condition=Q(battery_config__in=["", "0", "1", "2"]), name="packs_config_pack_battery_config_valid"),
            models.CheckConstraint(condition=Q(size_key__regex=SIZE_KEY), name="packs_config_pack_size_key_format"),
            models.CheckConstraint(condition=Q(future_size_key="") | Q(future_size_key__regex=SIZE_KEY), name="packs_config_pack_future_size_format"),
            models.CheckConstraint(condition=Q(key__regex=PACK_KEY), name="packs_config_pack_key_format"),
            models.CheckConstraint(condition=Q(size_kw__gt=0), name="packs_config_pack_size_positive"),
            models.CheckConstraint(condition=Q(is_future_ready=False, future_size_key="") | Q(is_future_ready=True) & ~Q(future_size_key=""), name="packs_config_pack_future_ready"),
        ]
        indexes = [models.Index(fields=["config_version", "system_type", "tier"], name="packs_config_pack_lookup")]

    def __str__(self) -> str:
        return self.key


class ConfigLine(BaseModel):
    # CASCADE: a line is a true child of its pack.
    pack = models.ForeignKey(ConfigPack, on_delete=models.CASCADE, related_name="lines")
    slot_key = models.CharField(max_length=32, help_text="The catalog category of the slot (panel, inverter …), 'fixed' or 'structure'.")
    # PROTECT: a component used by a pack is a catalog master. NULL for fixed items and structure material without one.
    component = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="pack_config_lines")
    name = models.CharField(max_length=255)
    qty = models.DecimalField(max_digits=12, decimal_places=3)
    source = models.CharField(max_length=10, choices=LineSource.choices)
    selection_method = models.CharField(max_length=40, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "packs_config_line"
        ordering = ["pack_id", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=in_choices("source", LineSource), name="packs_config_line_source_valid"),
            models.CheckConstraint(condition=Q(qty__gte=0), name="packs_config_line_qty_not_negative"),
            models.CheckConstraint(condition=~Q(source__in=[LineSource.SLOT, LineSource.MANUAL]) | Q(component__isnull=False), name="packs_config_line_slot_component"),
        ]
        indexes = [models.Index(fields=["component"], name="packs_config_line_component")]

    def __str__(self) -> str:
        return f"{self.slot_key}: {self.name} × {self.qty}"


class ConfigPin(BaseModel):
    """*(DV)* One pinned component of one slot of one pack (the platform replacement of the Flarize package registry)."""

    # CASCADE: a pin is a true child of its pack.
    pack = models.ForeignKey(ConfigPack, on_delete=models.CASCADE, related_name="pins")
    slot_key = models.CharField(max_length=32)
    # PROTECT: a pinned component is a catalog master.
    component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="pack_config_pins")
    authoritative = models.BooleanField(default=True, help_text="Flarize derivedBy 'explicit' (an approved decision) vs a derived fallback.")
    alternates = models.JSONField(default=list, blank=True, help_text="Component SKUs Sales may swap to when the slot lists none (registry approvedAlternates).")

    class Meta:
        db_table = "packs_config_pin"
        ordering = ["pack_id", "slot_key"]
        constraints = [models.UniqueConstraint(fields=["pack", "slot_key"], condition=LIVE, name="packs_config_pin_slot_uniq")]
        indexes = [models.Index(fields=["component"], name="packs_config_pin_component")]

    def __str__(self) -> str:
        return f"{self.slot_key} → {self.component_id}"
