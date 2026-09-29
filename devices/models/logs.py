"""``devices_sync_log`` *(no base)* and ``devices_protocol_mapping`` (PLAN §2.9).

A sync log is one read or upload of one terminal (INFO, USERS, ATTENDANCE). A **SUCCESS** USERS log is the presence
watermark: ``details.users_present`` lists the PINs that read found, and only such a log can conclude that anybody
left a terminal (a FAILED or PARTIAL read never does).

A protocol mapping assigns a meaning to a raw terminal code (``status`` / ``punch``) per platform and firmware; the raw
values stay untouched in the punches, so meaning can change without rewriting history.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import BaseModel


class SyncLog(models.Model):
    class Type(models.TextChoices):
        INFO = "INFO", "Device information"
        USERS = "USERS", "User table"
        ATTENDANCE = "ATTENDANCE", "Attendance"

    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    id = models.BigAutoField(primary_key=True)
    # A true child of the terminal: CASCADE (devices are only soft-deleted in practice).
    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.CASCADE, related_name="sync_logs")
    # Attribution: SET_NULL.
    agent = models.ForeignKey("devices.Agent", null=True, blank=True, on_delete=models.SET_NULL, related_name="sync_logs")
    sync_type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    records_read = models.PositiveIntegerField(default=0)
    records_new = models.PositiveIntegerField(default=0)
    records_duplicate = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "devices_sync_log"
        ordering = ["-started_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(sync_type__in=["INFO", "USERS", "ATTENDANCE"]), name="devices_sync_log_type_valid"),
            models.CheckConstraint(condition=Q(status__in=["SUCCESS", "PARTIAL", "FAILED"]), name="devices_sync_log_status_valid"),
        ]
        indexes = [
            # The presence watermark: latest USERS logs of one device.
            models.Index(fields=["device", "sync_type", "-started_at", "-id"], name="devices_sync_log_device_idx"),
            models.Index(fields=["agent", "-started_at"], name="devices_sync_log_agent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.sync_type} {self.status} device={self.device_id} at {self.started_at:%Y-%m-%d %H:%M:%S}"


class ProtocolMapping(BaseModel):
    class Field(models.TextChoices):
        STATUS = "status", "status"
        PUNCH = "punch", "punch"

    class MeaningType(models.TextChoices):
        VERIFY_MODE = "VERIFY_MODE", "Verify mode"
        PUNCH_DIRECTION = "PUNCH_DIRECTION", "Punch direction"

    class Confidence(models.TextChoices):
        VERIFIED = "VERIFIED", "Verified (controlled test)"
        ASSUMED = "ASSUMED", "Assumed"
        UNKNOWN = "UNKNOWN", "Unknown"

    device_platform = models.CharField(max_length=120, blank=True, default="", help_text="Empty = any platform.")
    firmware_version = models.CharField(max_length=120, blank=True, default="", help_text="Empty = any firmware.")
    field = models.CharField(max_length=30, choices=Field.choices)
    raw_value = models.IntegerField()
    meaning_type = models.CharField(max_length=30, choices=MeaningType.choices)
    meaning_code = models.CharField(max_length=40, help_text="FACE, CARD, FINGERPRINT, PASSWORD, IN, OUT, …")
    label = models.CharField(max_length=120, blank=True, default="")
    confidence = models.CharField(max_length=20, choices=Confidence.choices, default=Confidence.UNKNOWN)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "devices_protocol_mapping"
        ordering = ["field", "raw_value", "device_platform", "firmware_version", "id"]
        constraints = [
            models.UniqueConstraint(fields=["device_platform", "firmware_version", "field", "raw_value"], condition=Q(deleted_at__isnull=True), name="devices_protocol_mapping_scope_live_uniq"),
            models.CheckConstraint(condition=Q(field__in=["status", "punch"]), name="devices_protocol_mapping_field_valid"),
            models.CheckConstraint(condition=Q(meaning_type__in=["VERIFY_MODE", "PUNCH_DIRECTION"]), name="devices_protocol_mapping_meaning_type_valid"),
            models.CheckConstraint(condition=Q(confidence__in=["VERIFIED", "ASSUMED", "UNKNOWN"]), name="devices_protocol_mapping_confidence_valid"),
            models.CheckConstraint(condition=~Q(meaning_code=""), name="devices_protocol_mapping_meaning_not_blank"),
        ]

    def __str__(self) -> str:
        return f"{self.field}={self.raw_value} → {self.meaning_code}"
