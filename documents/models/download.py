"""``documents_download`` (PLAN §2.1): one issued single-use download link for a rendered document."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import BaseModel


class DocumentDownload(BaseModel):
    # CASCADE: a download link is a true child of its render job.
    job = models.ForeignKey("documents.RenderJob", on_delete=models.CASCADE, related_name="downloads")
    # Attribution: SET_NULL.
    issued_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_ip = models.GenericIPAddressField(null=True, blank=True)
    used_user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "documents_download"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["job", "created_at"], name="documents_download_job_created")]

    def __str__(self) -> str:
        return f"download {self.uid} of job {self.job_id}"
