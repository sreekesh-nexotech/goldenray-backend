"""``documents_render_job`` (PLAN §2.1): one HTML → PDF rendering of a document payload."""

from __future__ import annotations

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from core.models import BaseModel

LANGUAGES = ("en", "ml", "hi")


class RenderJob(BaseModel):
    class Kind(models.TextChoices):
        QUOTATION = "QUOTATION", "Quotation"
        AGREEMENT = "AGREEMENT", "Agreement"
        INSPECTION_REPORT = "INSPECTION_REPORT", "Inspection report"
        ATTENDANCE_REPORT = "ATTENDANCE_REPORT", "Attendance report"
        PUBLISH_REPORT = "PUBLISH_REPORT", "Publish report"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        DONE = "DONE", "Done"
        FAILED = "FAILED", "Failed"

    class Language(models.TextChoices):
        EN = "en", "English"
        ML = "ml", "Malayalam"
        HI = "hi", "Hindi"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    object_type = models.CharField(max_length=64)  # "<app_label>.<model>" of the record the document belongs to
    object_uid = models.UUIDField()
    template = models.CharField(max_length=64, default="default")
    language = models.CharField(max_length=2, choices=Language.choices)
    # The frozen, validated document payload the PDF is rendered from (DV-10): the task message carries only the job
    # uid, so a lost message or a crashed worker can be re-run from the row.
    payload = models.JSONField(encoder=DjangoJSONEncoder)
    payload_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    file = models.CharField(max_length=512, blank=True, default="")  # storage key of the PDF (private storage)
    # The private media asset holding the PDF (DV-10). SET_NULL: attribution-like link; the asset is never
    # hard-deleted while this row references it (media.usage).
    asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    page_count = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Attribution: SET_NULL.
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "documents_render_job"
        ordering = ["-created_at", "-id"]
        indexes = [
            # Documents of one record, newest first (quotation versions, agreements, reports).
            models.Index(fields=["object_type", "object_uid", "created_at"], name="documents_job_object_created"),
            # Sweeper and /healthz: the unfinished jobs, oldest first.
            models.Index(fields=["status", "created_at"], name="documents_job_open_created", condition=Q(status__in=["QUEUED", "RUNNING"])),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["QUOTATION", "AGREEMENT", "INSPECTION_REPORT", "ATTENDANCE_REPORT", "PUBLISH_REPORT"]), name="documents_job_kind_valid"),
            models.CheckConstraint(condition=Q(status__in=["QUEUED", "RUNNING", "DONE", "FAILED"]), name="documents_job_status_valid"),
            models.CheckConstraint(condition=Q(language__in=list(LANGUAGES)), name="documents_job_language_valid"),
            # A finished document always has its file.
            models.CheckConstraint(condition=~Q(status="DONE") | (~Q(file="") & Q(page_count__gte=1)), name="documents_job_done_has_file"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.object_type}:{self.object_uid} [{self.status}]"

    @property
    def is_finished(self) -> bool:
        return self.status in (self.Status.DONE, self.Status.FAILED)
