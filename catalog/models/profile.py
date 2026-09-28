"""``catalog_component_public_profile`` (PLAN §2.2): the website-facing marketing data that used to live in the legacy
``solar_panels`` / ``solar_inverters`` rows. Served by ``/api/public/<version>/products/…`` while PUBLISHED.

Beyond PLAN: ``image_url`` (legacy image path/URL; the media ``primary_image`` of the component wins),
``overall_rating`` and ``rating_tier`` are typed columns (website filters) instead of keys of ``ratings``
(which keeps the numeric 0–100 sub-ratings), ``price_range_label`` is 100 characters like the legacy column.
"""

from django.db import models
from django.db.models import Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
RATING_KEYS = ("efficiency", "heat_performance", "reliability", "warranty", "kerala_climate")


class ProfileStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"


class OverallRating(models.TextChoices):
    EXCELLENT = "EXCELLENT", "Excellent"
    VERY_GOOD = "VERY_GOOD", "Very good"
    GOOD = "GOOD", "Good"


class RatingTier(models.TextChoices):
    PREMIUM = "PREMIUM", "Premium"
    MID_RANGE = "MID_RANGE", "Mid-range"
    VALUE = "VALUE", "Value"


class ComponentPublicProfile(BaseModel):
    # CASCADE: the profile is part of its component.
    component = models.OneToOneField("catalog.Component", on_delete=models.CASCADE, related_name="public_profile")
    slug = models.SlugField(max_length=160)
    headline = models.CharField(max_length=200, blank=True, default="", help_text="Card title on the website.")
    summary = models.TextField(blank=True, default="")
    body = models.TextField(blank=True, default="", help_text="Markdown.")
    image_url = models.CharField(max_length=500, blank=True, default="", help_text="Legacy image path or URL; the component's primary image wins.")
    price_range_label = models.CharField(max_length=100, blank=True, default="", help_text="Shown when the current price release has no price for the component.")
    subsidy_eligible = models.BooleanField(null=True, blank=True)
    kerala_climate_score = models.PositiveSmallIntegerField(null=True, blank=True)
    overall_rating = models.CharField(max_length=12, choices=OverallRating.choices, null=True, blank=True)
    rating_tier = models.CharField(max_length=12, choices=RatingTier.choices, null=True, blank=True)
    ratings = models.JSONField(default=dict, blank=True, help_text="{efficiency, heat_performance, reliability, warranty, kerala_climate}: 0-100.")
    pros = models.JSONField(default=list, blank=True)
    cons = models.JSONField(default=list, blank=True)
    faq = models.JSONField(default=list, blank=True, help_text="[{question, answer}]")
    gallery = models.JSONField(default=list, blank=True, help_text="Public media asset uids.")
    seo_title = models.CharField(max_length=255, blank=True, default="")
    seo_description = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=ProfileStatus.choices, default=ProfileStatus.DRAFT)

    class Meta:
        db_table = "catalog_component_public_profile"
        ordering = ["-kerala_climate_score", "slug", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="catalog_profile_slug_live_uniq"),
            models.CheckConstraint(condition=Q(status__in=ProfileStatus.values), name="catalog_profile_status_valid"),
            models.CheckConstraint(condition=Q(overall_rating__isnull=True) | Q(overall_rating__in=OverallRating.values), name="catalog_profile_overall_rating_valid"),
            models.CheckConstraint(condition=Q(rating_tier__isnull=True) | Q(rating_tier__in=RatingTier.values), name="catalog_profile_rating_tier_valid"),
            models.CheckConstraint(condition=Q(kerala_climate_score__isnull=True) | Q(kerala_climate_score__lte=100), name="catalog_profile_kerala_score_range"),
            models.CheckConstraint(condition=~Q(status="PUBLISHED") | Q(published_at__isnull=False), name="catalog_profile_published_has_date"),
        ]
        indexes = [models.Index(fields=["status", "-kerala_climate_score"], name="catalog_profile_status_score")]

    def __str__(self) -> str:
        return self.slug
