"""``attendance_raw_punch`` *(no base; append-only)* — every punch a terminal delivered, exactly once (PLAN §2.9).

* **Append-only.** Nothing updates or deletes a punch: the model and its queryset refuse it, and in production the
  application role has ``UPDATE, DELETE, TRUNCATE`` revoked (``attendance.services.partitions``, like ``audit_log``).
* **Identity is the content** (A11): ``dedup_key`` = sha256 of ``serial|pin|device_time|status|punch`` — the same punch
  read by the office agent and pushed over ADMS is one row. Unique together with the partition key (below), which is
  the same thing: the key is a hash of ``device_time``.
* **Two clocks** (A10): ``device_time`` is the terminal's wall clock as reported (``timestamp``, never corrected);
  ``punch_at`` is that reading placed in the office's time zone (``timestamptz``) — the instant every computation uses.
* **Monthly partitions** on ``device_time`` (DV-91): the table, its DEFAULT partition, the unique key and the indexes
  are created by raw SQL in ``attendance/migrations/0001_initial.py``; the Django state mirrors them, so the model is
  managed (flushed with ``devices_device`` in tests) but its DDL is not Django's.
* A punch has no ``uid``: it is identified outside the service layer by its ``dedup_key``.
"""

from __future__ import annotations

from django.db import models
from django.db.models import BigIntegerField, Func, Q, Value
from django.utils import timezone

from attendance.models.fields import WallClockDateTimeField


class AppendOnlyError(RuntimeError):
    """Raw punches are never changed or removed."""


class RawPunchQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("attendance_raw_punch is append-only.")

    def delete(self):
        raise AppendOnlyError("attendance_raw_punch is append-only.")

    _raw_delete = delete


class RawPunch(models.Model):
    class Source(models.TextChoices):
        AGENT_PUSH = "AGENT_PUSH", "Office agent"
        ADMS_PUSH = "ADMS_PUSH", "ADMS push"
        IMPORT = "IMPORT", "Imported from eSSL"

    pk = models.CompositePrimaryKey("id", "device_time")
    # bigserial in the database (the sink inserts with raw SQL and reads the id back).
    id = models.BigIntegerField(db_default=Func(Value("attendance_raw_punch_id_seq"), function="nextval", output_field=BigIntegerField()), editable=False)
    # RESTRICT: a terminal with punches can never be hard-deleted (devices are only soft-deleted anyway).
    device = models.ForeignKey("devices.Device", on_delete=models.PROTECT, related_name="raw_punches")
    device_serial = models.CharField(max_length=80, blank=True, default="")
    device_record_uid = models.IntegerField(null=True, blank=True, help_text="The terminal's record uid (data, not identity: A11).")
    pin = models.CharField(max_length=80)
    device_time = WallClockDateTimeField(help_text="The terminal's wall clock as reported (never corrected).")
    punch_at = models.DateTimeField(help_text="device_time in the office's time zone (A10).")
    status_code = models.SmallIntegerField(null=True, blank=True)
    punch_code = models.SmallIntegerField(null=True, blank=True)
    source = models.CharField(max_length=12, choices=Source.choices)
    # Attribution: SET_NULL (agents are only soft-deleted, so this never rewrites the ledger).
    agent = models.ForeignKey("devices.Agent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # devices_adms_request is partitioned with a composite key: the evidence row id without a database FK (DV-91).
    adms_request_id = models.BigIntegerField(null=True, blank=True)
    dedup_key = models.CharField(max_length=64)
    raw_payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)

    objects = RawPunchQuerySet.as_manager()

    class Meta:
        db_table = "attendance_raw_punch"
        ordering = ["-punch_at", "-id"]
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["dedup_key", "device_time"], name="attendance_raw_punch_dedup_uniq"),
            models.CheckConstraint(condition=Q(source__in=["AGENT_PUSH", "ADMS_PUSH", "IMPORT"]), name="attendance_raw_punch_source_valid"),
            models.CheckConstraint(condition=~Q(pin=""), name="attendance_raw_punch_pin_not_blank"),
            models.CheckConstraint(condition=Q(dedup_key__regex=r"^[0-9a-f]{64}$"), name="attendance_raw_punch_dedup_key_format"),
        ]
        indexes = [
            models.Index(fields=["device", "pin", "device_time"], name="attendance_raw_punch_pin_idx"),
            models.Index(fields=["punch_at"], name="attendance_raw_punch_at_idx"),
            models.Index(fields=["agent"], name="attendance_raw_punch_agent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.device_serial or self.device_id}:{self.pin} at {self.device_time:%Y-%m-%d %H:%M:%S}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("attendance_raw_punch is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("attendance_raw_punch is append-only.")
