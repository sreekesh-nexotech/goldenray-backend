"""``seo_page_metadata`` — per-route page metadata (replaces the main backend's ``goldenray.Metadata``).

Every legacy column has a typed home (DV-18): ``page`` (the route key the frontend asks for — ``home``,
``projects/123``), ``title``, ``description``, ``keywords`` (a string list, was ``jsonb``), ``imageUrl`` →
``og_image_url``, ``ogtype`` → ``og_type``. An uploaded ``og_image`` (public media) wins over the URL.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
PAGE_KEY_REGEX = r"^[a-z0-9][a-z0-9._~-]*(/[a-z0-9._~-]+)*$"


class PageMetadata(BaseModel):
    page = models.CharField(max_length=200, help_text="route key without leading/trailing slash; 'home' for /")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    keywords = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    og_type = models.CharField(max_length=32, default="website")
    og_image_url = models.CharField(max_length=500, blank=True, default="", help_text="site-relative or absolute image URL")
    # SET_NULL: optional; media.usage refuses deleting an asset a live row references.
    og_image = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "seo_page_metadata"
        ordering = ["page", "id"]
        constraints = [
            models.UniqueConstraint(fields=["page"], condition=LIVE, name="seo_page_metadata_page_live_uniq"),
            models.CheckConstraint(condition=Q(page__regex=PAGE_KEY_REGEX), name="seo_page_metadata_page_format"),
            models.CheckConstraint(condition=Q(og_type__regex=r"^[a-z][a-z0-9._:-]*$"), name="seo_page_metadata_og_type_format"),
        ]

    def __str__(self) -> str:
        return f"{self.page}: {self.title}"
