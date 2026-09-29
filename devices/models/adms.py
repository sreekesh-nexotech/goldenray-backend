"""ADMS evidence (PLAN §2.9): ``devices_adms_request`` *(no base)* and ``devices_adms_unknown_device``.

``devices_adms_request`` records every ``/iclock/<device_token>/…`` request exactly as it arrived, before (and
besides) anything it is interpreted as: a parser bug can never destroy the only record of what a terminal sent. It is
range-partitioned by month on ``received_at`` (created by raw SQL in ``devices/migrations/0001_initial.py``; the model
is **unmanaged**), the stored body is capped at 1 MiB (``body_bytes`` keeps the real size), biometric payloads
(ATTPHOTO / BIODATA) are never stored, and rows older than ``DEVICES_ADMS_RETENTION_DAYS`` (30) are purged daily by
``devices.tasks.purge_adms_evidence`` (whole monthly partitions are dropped once they are entirely out of the window).
The path is stored with the device token redacted.

``devices_adms_unknown_device`` is the quarantine list: a serial that pushed without a registered device behind it
(``UNKNOWN_SERIAL``) or with the wrong device token (``TOKEN_MISMATCH``). Nothing it sent is ingested, and no device
row is ever created from it.
"""

from __future__ import annotations

from django.db import models
from django.db.models import BigIntegerField, Func, Q, Value
from django.utils import timezone

from core.models import BaseModel

MAX_STORED_BODY_BYTES = 1024 * 1024
BODY_EXCERPT_CHARS = 4000


class AdmsRequest(models.Model):
    class Kind(models.TextChoices):
        HANDSHAKE = "HANDSHAKE", "Handshake"
        ATTLOG = "ATTLOG", "Attendance log"
        OPERLOG = "OPERLOG", "Operation log"
        USERINFO = "USERINFO", "User information"
        ATTPHOTO = "ATTPHOTO", "Attendance photo"
        BIODATA = "BIODATA", "Biometric template"
        CDATA = "CDATA", "Other table"
        GETREQUEST = "GETREQUEST", "Command poll"
        DEVICECMD = "DEVICECMD", "Command result"
        REGISTRY = "REGISTRY", "Registry"
        PING = "PING", "Ping"
        UNKNOWN = "UNKNOWN", "Unknown"
        UNSTORABLE = "UNSTORABLE", "Unstorable"

    pk = models.CompositePrimaryKey("id", "received_at")
    # bigserial in the database; the receiver reserves the id with nextval() before it writes the row.
    id = models.BigIntegerField(db_default=Func(Value("devices_adms_request_id_seq"), function="nextval", output_field=BigIntegerField()), editable=False)
    received_at = models.DateTimeField(default=timezone.now)
    client_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Trusted client address (X-Forwarded-For only through TRUSTED_PROXIES).")
    peer_ip = models.GenericIPAddressField(null=True, blank=True)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255, help_text="Request path with the device token redacted.")
    raw_query = models.TextField(blank=True, default="")
    query = models.JSONField(default=dict, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    content_type = models.CharField(max_length=160, blank=True, default="")
    device_serial = models.CharField(max_length=80, blank=True, default="", help_text="The serial the request claimed (SN).")
    # Evidence of which registered device it resolved to. No database FK (DV-76: the append-only partitioned log must
    # not block flushing or dropping devices and partitions); devices are only soft-deleted, so the id stays resolvable.
    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", db_constraint=False)
    request_kind = models.CharField(max_length=30, choices=Kind.choices, default=Kind.UNKNOWN)
    table_name = models.CharField(max_length=40, blank=True, default="")
    body = models.BinaryField(null=True, blank=True)
    body_bytes = models.PositiveIntegerField(default=0, help_text="Size of the body as received (the stored copy is capped at 1 MiB).")
    body_text = models.TextField(blank=True, default="")
    body_encoding = models.CharField(max_length=30, blank=True, default="")
    body_truncated = models.BooleanField(default=False)
    response_status = models.PositiveSmallIntegerField(default=200)
    response_body = models.TextField(blank=True, default="")
    records_parsed = models.PositiveIntegerField(default=0)
    records_new = models.PositiveIntegerField(default=0)
    records_duplicate = models.PositiveIntegerField(default=0)
    records_invalid = models.PositiveIntegerField(default=0)
    parse_error = models.TextField(blank=True, default="")
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        managed = False
        db_table = "devices_adms_request"
        ordering = ["-received_at", "-id"]
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.request_kind} {self.device_serial or '-'} at {self.received_at:%Y-%m-%d %H:%M:%S}"


class AdmsUnknownDevice(BaseModel):
    class Reason(models.TextChoices):
        UNKNOWN_SERIAL = "UNKNOWN_SERIAL", "No registered device has this serial"
        TOKEN_MISMATCH = "TOKEN_MISMATCH", "The serial and the device token do not belong together"

    serial_number = models.CharField(max_length=80)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    request_count = models.PositiveIntegerField(default=0)
    last_source_ip = models.GenericIPAddressField(null=True, blank=True)
    last_path = models.CharField(max_length=255, blank=True, default="")
    last_body_excerpt = models.TextField(blank=True, default="")
    last_reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.UNKNOWN_SERIAL)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "devices_adms_unknown_device"
        ordering = ["-last_seen_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["serial_number"], condition=Q(deleted_at__isnull=True), name="devices_adms_unknown_serial_live_uniq"),
            models.CheckConstraint(condition=~Q(serial_number=""), name="devices_adms_unknown_serial_not_blank"),
            models.CheckConstraint(condition=Q(last_reason__in=["UNKNOWN_SERIAL", "TOKEN_MISMATCH"]), name="devices_adms_unknown_reason_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.serial_number} ({self.request_count} requests)"
