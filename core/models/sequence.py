from django.db import models
from django.db.models import Q


class SequenceCounter(models.Model):
    """Gap-free document counters (no base). Read and advanced with ``SELECT … FOR UPDATE`` (core.sequences)."""

    pk = models.CompositePrimaryKey("kind", "period_key")
    kind = models.CharField(max_length=16)
    period_key = models.CharField(max_length=16, blank=True, default="")
    next_value = models.BigIntegerField(default=1)

    class Meta:
        db_table = "core_sequence_counter"
        constraints = [models.CheckConstraint(condition=Q(next_value__gte=1), name="core_sequence_counter_next_value_positive")]

    def __str__(self):
        return f"{self.kind}:{self.period_key or '-'}={self.next_value}"
