from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class OutboxEvent(models.Model):
    """Transactional outbox (no base). Written in the producer's transaction; drained by ``core.tasks.drain_outbox``.

    ``processed_at`` is set when a drainer claims the row (before dispatch); on handler failure it is cleared again
    for a retry, or the row is parked (``parked_at``) after ``OUTBOX_MAX_ATTEMPTS``. ``delivered`` lists the
    handlers that already succeeded, so a retry never re-runs them. ``dedup_key`` makes emits idempotent.
    """

    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=64)
    aggregate_type = models.CharField(max_length=64, blank=True, default="")
    aggregate_uid = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    dedup_key = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    parked_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    delivered = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "core_outbox_event"
        constraints = [models.UniqueConstraint(fields=["dedup_key"], condition=Q(dedup_key__isnull=False), name="core_outbox_event_dedup_key_uniq")]
        indexes = [
            models.Index(fields=["id"], condition=Q(processed_at__isnull=True, parked_at__isnull=True), name="core_outbox_pending_idx"),
            models.Index(fields=["aggregate_type", "aggregate_uid"], name="core_outbox_aggregate_idx"),
            models.Index(fields=["parked_at"], condition=Q(parked_at__isnull=False), name="core_outbox_parked_idx"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.event_type}"
