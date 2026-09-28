from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class OutboxEvent(models.Model):
    """Transactional outbox (no base). Written in the producer's transaction; drained by ``core.tasks.drain_outbox``.

    Row states (DV-7):

    * **pending** — ``processed_at`` and ``parked_at`` are NULL;
    * **claimed** — a drainer holds a lease until ``claimed_until`` (set before dispatch). A lease that expires
      without an outcome (the drainer died) makes the row claimable again;
    * **retrying** — a handler failed: ``next_attempt_at`` holds the exponential backoff, ``last_error`` the reason;
    * **processed** — ``processed_at`` is set only once every handler succeeded (``delivered`` lists them, so a
      retry never re-runs one);
    * **parked** — ``parked_at`` after ``OUTBOX_MAX_ATTEMPTS`` failed or abandoned claims (poison pill); ops re-drive
      it with ``manage.py drain_outbox --requeue-parked``.

    ``dedup_key`` makes emits idempotent.
    """

    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=64)
    aggregate_type = models.CharField(max_length=64, blank=True, default="")
    aggregate_uid = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    dedup_key = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)  # completion: every handler delivered
    claimed_until = models.DateTimeField(null=True, blank=True)  # lease of the drainer dispatching the row
    next_attempt_at = models.DateTimeField(null=True, blank=True)  # retry backoff after a failed dispatch
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
