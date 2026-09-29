"""``pricing_release`` + ``pricing_release_line`` — immutable PriceReleases (PLAN §1.2, §2.3).

A release is written once by ``pricing.services.releases.publish`` and never edited; a correction is a new release.
The current release is the single PUBLISHED row (partial unique index), which is also the highest number: publishing
sets the previous one SUPERSEDED in the same transaction. ``payload`` is the full snapshot (component prices by SKU,
market rates, swap deltas, roof add-ons, cost configuration, installation matrix, statutory fees, validity, GST) and
``payload_sha256`` its canonical hash.

Columns beyond the PLAN list (DV): ``superseded_at``; ``publish_report`` (the report the publisher saw: warnings
accepted at publish time); on lines ``list_price_gst_inclusive`` (whether the list price already includes GST).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from pricing.models.choices import ReleaseStatus, in_choices


class PriceRelease(BaseModel):
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=ReleaseStatus.choices, default=ReleaseStatus.PUBLISHED)
    published_at = models.DateTimeField()
    # SET_NULL: attribution only.
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)
    # PROTECT: the market rate set a release was built from can never disappear.
    market_rate_set = models.ForeignKey("pricing.MarketRateSet", on_delete=models.PROTECT, related_name="releases")
    note = models.TextField(blank=True, default="")
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    publish_report = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "pricing_release"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="pricing_release_number_uniq"),
            models.UniqueConstraint(fields=["status"], condition=Q(status=ReleaseStatus.PUBLISHED), name="pricing_release_one_published"),
            models.CheckConstraint(condition=in_choices("status", ReleaseStatus), name="pricing_release_status_valid"),
            models.CheckConstraint(condition=Q(number__gte=1), name="pricing_release_number_positive"),
            models.CheckConstraint(condition=Q(payload_sha256__regex=r"^[0-9a-f]{64}$"), name="pricing_release_sha256_format"),
        ]

    def __str__(self) -> str:
        return f"PriceRelease #{self.number} ({self.status})"


class PriceReleaseLine(models.Model):
    """*(no base)* One component's prices in a release, for querying without reading the payload."""

    pk = models.CompositePrimaryKey("release", "component")
    # CASCADE: a line is a true child of its release (releases are never deleted).
    release = models.ForeignKey(PriceRelease, on_delete=models.CASCADE, related_name="lines")
    # PROTECT: a released component can never disappear.
    component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="release_lines")
    list_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    list_price_gst_inclusive = models.BooleanField(default=False)
    landed_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=4)

    class Meta:
        db_table = "pricing_release_line"
        ordering = ["release_id", "component_id"]
        constraints = [
            models.CheckConstraint(condition=Q(gst_rate__gte=0) & Q(gst_rate__lte=1), name="pricing_release_line_gst_fraction"),
            models.CheckConstraint(condition=Q(list_price__isnull=True) | Q(list_price__gte=0), name="pricing_release_line_list_not_negative"),
            models.CheckConstraint(condition=Q(landed_cost__isnull=True) | Q(landed_cost__gte=0), name="pricing_release_line_landed_not_negative"),
        ]
        indexes = [models.Index(fields=["component", "release"], name="pricing_release_line_comp")]

    def __str__(self) -> str:
        return f"#{self.release_id}/{self.component_id}"
