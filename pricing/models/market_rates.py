"""Market rate sets and their children (PLAN §2.3): rates, swap deltas, roof add-ons.

``pricing_market_rate_set`` — exactly one ACTIVE set at a time (partial unique index); only DRAFT sets are edited;
activating a set retires the previous ACTIVE one in the same transaction.

``pricing_market_rate`` — one customer price (GST inclusive) per (system type, tier, battery band, size) of a set. It
replaces ``bom.MarketRate.size_rates`` and Flarize ``marketRates`` losslessly (DV, mapping in
docs/decisions/pricing-procurement.md):

* ``size_key`` — the source size key exactly (``3``, ``5sp``, ``5tp``, ``10``); ``size_kw`` is its kW and ``phase``
  its phase (``sp`` → 1P, ``tp`` → 3P) — two 5 kW rows (1P and 3P) must coexist;
* ``from_size_key`` / ``from_size_kw`` — the upgrade path's starting size (``bom.MarketRate.from_size``; the target is
  ``size_key``);
* ``future_size_key`` — Flarize future-ready packs (``ongrid_value_up5sp``: panels at ``size_key``, the rest at
  ``future_size_key``); ``variant`` — any other key suffix Flarize carries (``hybrid_value_2_diffBase``);
* ``sort_order`` — the source's key order inside ``size_rates``; ``0`` means "not set" (both sources read 0 as
  missing, and the publish report lists it).
"""

from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from core.models import BaseModel
from pricing.models.choices import LIVE, BatteryConfig, MarketRateSetStatus, Phase, StructureType, SwapSlot, SystemType, Tier, in_choices

TIER_OR_BLANK = Q(tier="") | in_choices("tier", Tier)


class MarketRateSet(BaseModel):
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=10, choices=MarketRateSetStatus.choices, default=MarketRateSetStatus.DRAFT)
    activated_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "pricing_market_rate_set"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["status"], condition=LIVE & Q(status=MarketRateSetStatus.ACTIVE), name="pricing_market_rate_set_one_active"),
            models.UniqueConstraint(Lower("name"), condition=LIVE, name="pricing_market_rate_set_name_live_uniq"),
            models.CheckConstraint(condition=in_choices("status", MarketRateSetStatus), name="pricing_market_rate_set_status_valid"),
            models.CheckConstraint(condition=~Q(name=""), name="pricing_market_rate_set_name_not_blank"),
        ]
        indexes = [models.Index(fields=["status", "-created_at"], name="pricing_mrs_status_created")]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"


class MarketRate(BaseModel):
    # CASCADE: a rate is a true child of its set.
    set = models.ForeignKey(MarketRateSet, on_delete=models.CASCADE, related_name="rates")
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, blank=True, default="", help_text="Blank only for legacy upgrade rows (no tier).")
    battery_config = models.CharField(max_length=4, blank=True, default="", choices=BatteryConfig.choices)
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    size_key = models.CharField(max_length=8, help_text="Source size key: 3, 5sp, 5tp, 10 …")
    phase = models.CharField(max_length=4, blank=True, default="", choices=Phase.choices)
    from_size_key = models.CharField(max_length=8, blank=True, default="", help_text="UPGRADE: starting size key.")
    from_size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    future_size_key = models.CharField(max_length=8, blank=True, default="", help_text="Future-ready pack: system size key (Flarize _up<size>).")
    variant = models.CharField(max_length=24, blank=True, default="", help_text="Any other Flarize key suffix (e.g. diffBase).")
    customer_price_incl_gst = models.DecimalField(max_digits=14, decimal_places=2, help_text="0 = not set.")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "pricing_market_rate"
        ordering = ["set_id", "system_type", "tier", "battery_config", "future_size_key", "variant", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["set", "system_type", "tier", "battery_config", "size_key", "from_size_key", "future_size_key", "variant"],
                condition=LIVE,
                name="pricing_market_rate_cell_uniq",
            ),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="pricing_market_rate_system_valid"),
            models.CheckConstraint(condition=TIER_OR_BLANK, name="pricing_market_rate_tier_valid"),
            models.CheckConstraint(condition=in_choices("battery_config", BatteryConfig), name="pricing_market_rate_battery_valid"),
            models.CheckConstraint(condition=in_choices("phase", Phase), name="pricing_market_rate_phase_valid"),
            models.CheckConstraint(condition=Q(size_kw__gt=0), name="pricing_market_rate_size_positive"),
            models.CheckConstraint(condition=Q(customer_price_incl_gst__gte=0), name="pricing_market_rate_price_not_negative"),
            models.CheckConstraint(
                condition=(Q(system_type=SystemType.UPGRADE) & ~Q(from_size_key="")) | (~Q(system_type=SystemType.UPGRADE) & Q(from_size_key="") & ~Q(tier="")),
                name="pricing_market_rate_upgrade_shape",
            ),
            models.CheckConstraint(condition=~Q(size_key=""), name="pricing_market_rate_size_key_not_blank"),
        ]
        indexes = [models.Index(fields=["set", "system_type", "tier"], name="pricing_mr_set_system_tier")]

    def __str__(self) -> str:
        return f"{self.system_type}/{self.tier}/{self.battery_config}/{self.size_key} = {self.customer_price_incl_gst}"


class SwapDelta(BaseModel):
    """``pricing_swap_delta`` — Flarize ``Σ swapDelta``: the GST-inclusive price change of swapping a default component."""

    # CASCADE: child of its set.
    set = models.ForeignKey(MarketRateSet, on_delete=models.CASCADE, related_name="swap_deltas")
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    slot = models.CharField(max_length=24, choices=SwapSlot.choices)
    # PROTECT: a swap references catalog components (masters).
    from_component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="+")
    to_component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="+")
    delta_incl_gst = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = "pricing_swap_delta"
        ordering = ["set_id", "system_type", "tier", "slot", "id"]
        constraints = [
            models.UniqueConstraint(fields=["set", "system_type", "tier", "slot", "from_component", "to_component"], condition=LIVE, name="pricing_swap_delta_uniq"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="pricing_swap_delta_system_valid"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="pricing_swap_delta_tier_valid"),
            models.CheckConstraint(condition=in_choices("slot", SwapSlot), name="pricing_swap_delta_slot_valid"),
            models.CheckConstraint(condition=~Q(from_component=F("to_component")), name="pricing_swap_delta_distinct_components"),
        ]

    def __str__(self) -> str:
        return f"{self.slot}: {self.from_component_id} → {self.to_component_id} = {self.delta_incl_gst}"


class RoofAddon(BaseModel):
    """``pricing_roof_addon`` — GST-inclusive add-on for a non-flat structure at a size."""

    # CASCADE: child of its set.
    set = models.ForeignKey(MarketRateSet, on_delete=models.CASCADE, related_name="roof_addons")
    structure_type = models.CharField(max_length=16, choices=StructureType.choices)
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    addon_incl_gst = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        db_table = "pricing_roof_addon"
        ordering = ["set_id", "structure_type", "size_kw", "id"]
        constraints = [
            models.UniqueConstraint(fields=["set", "structure_type", "size_kw"], condition=LIVE, name="pricing_roof_addon_uniq"),
            models.CheckConstraint(condition=in_choices("structure_type", StructureType), name="pricing_roof_addon_structure_valid"),
            models.CheckConstraint(condition=Q(size_kw__gt=0), name="pricing_roof_addon_size_positive"),
            models.CheckConstraint(condition=Q(addon_incl_gst__gte=0), name="pricing_roof_addon_amount_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.structure_type}/{self.size_kw} = {self.addon_incl_gst}"
