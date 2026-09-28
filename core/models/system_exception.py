from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class SystemException(models.Model):
    """Unexpected exceptions (no base). Written by a never-raising writer (core.services.system_exceptions)."""

    class Source(models.TextChoices):
        API = "api", "API request"
        TASK = "task", "Celery task"
        OUTBOX = "outbox", "Outbox handler"
        COMMAND = "command", "Management command"

    id = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.API)
    request_id = models.UUIDField(null=True, blank=True)
    method = models.CharField(max_length=8, blank=True, default="")
    path = models.CharField(max_length=512, blank=True, default="")
    user_uid = models.UUIDField(null=True, blank=True)
    exception_type = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    traceback = models.TextField(blank=True, default="")
    context = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        db_table = "core_system_exception"
        constraints = [models.CheckConstraint(condition=Q(source__in=["api", "task", "outbox", "command"]), name="core_system_exception_source_valid")]
        indexes = [models.Index(fields=["-occurred_at"], name="core_sysexc_occurred_idx"), models.Index(fields=["exception_type", "occurred_at"], name="core_sysexc_type_idx")]

    def __str__(self):
        return f"{self.exception_type} at {self.occurred_at:%Y-%m-%d %H:%M:%S}"
