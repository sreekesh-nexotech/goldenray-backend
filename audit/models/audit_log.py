"""``audit_log`` — the append-only audit ledger (PLAN §2.1).

The table is created by ``audit/migrations/0001_initial.py`` with raw SQL because Django cannot declare a
range-partitioned table: ``PARTITION BY RANGE (at)`` with monthly partitions (``audit_log_yYYYYmMM``) plus a
``DEFAULT`` partition, primary key ``(id, at)`` (a partitioned table's key must contain the partition column),
``id bigserial`` and the indexes ``(object_type, object_uid, at)``, ``(actor_id, at)``, ``(action, at)``. This model
is therefore **unmanaged**: Django maps it but never creates or alters the table.

Append-only is enforced twice:

* in the database — ``UPDATE, DELETE, TRUNCATE`` are revoked from the application role (``DB_APP_ROLE``) whenever
  it differs from the owner (see ``audit.services.partitions`` and ``docs/ops/audit-log.md``);
* in the application — this model refuses to save an existing row or delete anything, and its queryset refuses
  ``update()``/``delete()``. Rows are written only by ``audit.services.record``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import BigIntegerField, Func, Value
from django.utils import timezone


class AppendOnlyError(RuntimeError):
    """Raised when code tries to change or remove an audit row."""


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("audit_log is append-only: rows are never updated.")

    def delete(self):
        raise AppendOnlyError("audit_log is append-only: rows are never deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AppendOnlyError("audit_log is append-only: rows are never updated.")

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        raise AppendOnlyError("audit_log is append-only: rows are never updated.")

    def _raw_delete(self, using):
        raise AppendOnlyError("audit_log is append-only: rows are never deleted.")

    _raw_delete.queryset_only = True


class AuditLog(models.Model):
    """One audited action. *(no base)*: ``id`` bigserial + ``at``; no ``uid``, no soft delete, never updated."""

    class ActorKind(models.TextChoices):
        USER = "USER", "User"
        AGENT = "AGENT", "Office agent"
        DEVICE = "DEVICE", "Device"
        CUSTOMER = "CUSTOMER", "Customer"
        SYSTEM = "SYSTEM", "System"

    pk = models.CompositePrimaryKey("id", "at")
    # bigserial in the database; Django sends DEFAULT on insert and reads the value back with RETURNING.
    id = models.BigIntegerField(db_default=Func(Value("audit_log_id_seq"), function="nextval", output_field=BigIntegerField()), editable=False)
    at = models.DateTimeField(default=timezone.now)
    # Attribution (the PLAN's SET_NULL relation). No database constraint and DO_NOTHING (DV-9): any ON DELETE action
    # would UPDATE ledger rows. Users are only ever soft-deleted, so the reference stays valid.
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.DO_NOTHING, related_name="+", db_constraint=False)
    actor_kind = models.CharField(max_length=8, choices=ActorKind.choices, default=ActorKind.SYSTEM)
    request_id = models.UUIDField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True, default="")
    object_uid = models.UUIDField(null=True, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        managed = False
        db_table = "audit_log"
        default_permissions = ()

    def __str__(self):
        return f"{self.action} {self.object_type}:{self.object_uid or '-'} at {self.at:%Y-%m-%d %H:%M:%S}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("audit_log is append-only: rows are never updated.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("audit_log is append-only: rows are never deleted.")
