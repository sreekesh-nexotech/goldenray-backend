"""``packs_release`` + ``packs_release_pack`` — immutable PackReleases (PLAN §1.2, §2.5).

A release is built from the current APPROVED/PUBLISHED config version and the current PriceRelease by
``packs.services.releases.publish`` and never edited; the current release is the single PUBLISHED row (the highest
number). ``packs_release_pack`` is the registry the website, calculators and quotations read: one priced pack per
(system type, tier, size, phase, battery configuration, future-ready size) with its BOM.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from packs.models.choices import Phase, ReleaseStatus, SystemType, Tier, in_choices
from packs.models.config import PACK_KEY, SIZE_KEY


class PackRelease(BaseModel):
    number = models.PositiveIntegerField()
    # PROTECT: a release keeps the configuration and prices it was built from.
    config_version = models.ForeignKey("packs.ConfigVersion", on_delete=models.PROTECT, related_name="releases")
    price_release = models.ForeignKey("pricing.PriceRelease", on_delete=models.PROTECT, related_name="pack_releases")
    content_release_uid = models.UUIDField(null=True, blank=True, help_text="The quotation ContentRelease (quotations_content_version) when one is published (DV).")
    status = models.CharField(max_length=10, choices=ReleaseStatus.choices, default=ReleaseStatus.PUBLISHED)
    published_at = models.DateTimeField()
    # SET_NULL: attribution only.
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    publish_report = models.JSONField(default=dict, blank=True)
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)

    class Meta:
        db_table = "packs_release"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="packs_release_number_uniq"),
            models.UniqueConstraint(fields=["status"], condition=Q(status=ReleaseStatus.PUBLISHED), name="packs_release_one_published"),
            models.CheckConstraint(condition=in_choices("status", ReleaseStatus), name="packs_release_status_valid"),
            models.CheckConstraint(condition=Q(number__gte=1), name="packs_release_number_positive"),
            models.CheckConstraint(condition=Q(payload_sha256__regex=r"^[0-9a-f]{64}$"), name="packs_release_sha256_format"),
        ]

    def __str__(self) -> str:
        return f"PackRelease #{self.number} ({self.status})"


class ReleasePack(models.Model):
    """*(no base)* One priced pack of a release."""

    pk = models.CompositePrimaryKey("release", "system_type", "tier", "size_key", "phase", "battery_config", "future_size_key")
    # CASCADE: a pack is a true child of its release (releases are never deleted).
    release = models.ForeignKey(PackRelease, on_delete=models.CASCADE, related_name="packs")
    key = models.CharField(max_length=64)
    system_type = models.CharField(max_length=8, choices=SystemType.choices)
    tier = models.CharField(max_length=8, choices=Tier.choices)
    size_key = models.CharField(max_length=10)
    size_kw = models.DecimalField(max_digits=6, decimal_places=2)
    phase = models.CharField(max_length=2, choices=Phase.choices)
    battery_config = models.CharField(max_length=4, blank=True, default="")
    future_size_key = models.CharField(max_length=10, blank=True, default="")
    future_size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    market_rate_key = models.CharField(max_length=48)
    customer_price_incl_gst = models.DecimalField(max_digits=14, decimal_places=2)
    customer_price_excl_gst = models.DecimalField(max_digits=14, decimal_places=2)
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2)
    landed_cost_total = models.DecimalField(max_digits=14, decimal_places=2)
    gross_margin_pct = models.DecimalField(max_digits=10, decimal_places=4, help_text="Fraction: (price excl. GST − landed cost) / price excl. GST.")
    bom = models.JSONField(help_text="Lines: sku, name, category, qty, unit list price, amount, GST rate/amount, source.")
    pricing = models.JSONField(default=dict, blank=True, help_text="engines.pack_pricing result (FLAT roof, no transport); `internal` is internal data.")
    display_name = models.CharField(max_length=120)
    profile_key = models.CharField(max_length=32, blank=True, default="")
    engineering_result = models.CharField(max_length=8, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "packs_release_pack"
        ordering = ["release_id", "sort_order"]
        constraints = [
            models.UniqueConstraint(fields=["release", "key"], name="packs_release_pack_key_uniq"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="packs_release_pack_system_type_valid"),
            models.CheckConstraint(condition=in_choices("tier", Tier), name="packs_release_pack_tier_valid"),
            models.CheckConstraint(condition=in_choices("phase", Phase), name="packs_release_pack_phase_valid"),
            models.CheckConstraint(condition=Q(battery_config__in=["", "0", "1", "2"]), name="packs_release_pack_battery_config_valid"),
            models.CheckConstraint(condition=Q(size_key__regex=SIZE_KEY), name="packs_release_pack_size_key_format"),
            models.CheckConstraint(condition=Q(key__regex=PACK_KEY), name="packs_release_pack_key_format"),
            models.CheckConstraint(condition=Q(customer_price_incl_gst__gt=0), name="packs_release_pack_price_positive"),
            models.CheckConstraint(condition=Q(customer_price_excl_gst__gte=0) & Q(gst_amount__gte=0) & Q(landed_cost_total__gte=0), name="packs_release_pack_amounts_not_negative"),
        ]
        indexes = [models.Index(fields=["release", "system_type", "tier", "size_kw"], name="packs_release_pack_lookup")]

    def __str__(self) -> str:
        return f"#{self.release_id}/{self.key}"
