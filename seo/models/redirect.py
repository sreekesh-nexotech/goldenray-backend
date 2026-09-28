"""``seo_redirect`` — site redirects exported to the Next.js build (``next.config`` ``redirects()``)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class Redirect(BaseModel):
    class StatusCode(models.IntegerChoices):
        MOVED_PERMANENTLY = 301, "301 Moved permanently"
        FOUND = 302, "302 Found (temporary)"
        TEMPORARY_REDIRECT = 307, "307 Temporary redirect"
        PERMANENT_REDIRECT = 308, "308 Permanent redirect"

    from_path = models.CharField(max_length=500, help_text="site-relative source path, e.g. /old-blog/:slug")
    to_path = models.CharField(max_length=1000, help_text="site-relative path or absolute https URL")
    status_code = models.PositiveSmallIntegerField(choices=StatusCode.choices, default=StatusCode.PERMANENT_REDIRECT)
    hits = models.PositiveIntegerField(default=0, help_text="reserved for a future hit counter (redirects run inside the Next.js build)")
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "seo_redirect"
        ordering = ["from_path", "id"]
        constraints = [
            models.UniqueConstraint(fields=["from_path"], condition=LIVE, name="seo_redirect_from_path_live_uniq"),
            models.CheckConstraint(condition=Q(status_code__in=[301, 302, 307, 308]), name="seo_redirect_status_code_valid"),
            models.CheckConstraint(condition=Q(from_path__startswith="/") & ~Q(from_path__startswith="//"), name="seo_redirect_from_path_site_relative"),
            models.CheckConstraint(condition=~Q(from_path=models.F("to_path")), name="seo_redirect_not_to_itself"),
        ]

    def __str__(self) -> str:
        return f"{self.from_path} → {self.to_path} ({self.status_code})"

    @property
    def permanent(self) -> bool:
        return self.status_code in (301, 308)
