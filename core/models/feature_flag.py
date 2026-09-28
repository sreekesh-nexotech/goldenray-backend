from django.db import models
from django.db.models import Q

from core.models.base import BaseModel


class FeatureFlag(BaseModel):
    """Runtime switch. Rows override ``settings.FEATURE_FLAG_DEFAULTS`` (every default is off)."""

    key = models.CharField(max_length=64)
    enabled = models.BooleanField(default=False)
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "core_feature_flag"
        constraints = [models.UniqueConstraint(fields=["key"], condition=Q(deleted_at__isnull=True), name="core_feature_flag_key_uniq")]

    def __str__(self):
        return f"{self.key}={'on' if self.enabled else 'off'}"
