from django.db import models
from django.db.models import Q
from django.utils import timezone


class LegacyMap(models.Model):
    """Source row → platform row (no base). Makes every importer idempotent and every migrated row traceable."""

    class SourceSystem(models.TextChoices):
        CMS = "CMS", "CMS (blog_cms)"
        BACKEND = "BACKEND", "Main backend (GoldenApp)"
        FLARIZE = "FLARIZE", "Flarize JSON"
        PA = "PA", "Purchase Agreement page"
        SI = "SI", "Site Inspection V2"
        ESSL = "ESSL", "eSSL attendance"

    id = models.BigAutoField(primary_key=True)
    source_system = models.CharField(max_length=16, choices=SourceSystem.choices)
    source_table = models.CharField(max_length=64)
    source_id = models.CharField(max_length=128)
    target_table = models.CharField(max_length=64)
    target_id = models.BigIntegerField()
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "core_legacy_map"
        constraints = [
            models.UniqueConstraint(fields=["source_system", "source_table", "source_id"], name="core_legacy_map_source_uniq"),
            models.CheckConstraint(condition=Q(source_system__in=["CMS", "BACKEND", "FLARIZE", "PA", "SI", "ESSL"]), name="core_legacy_map_source_system_valid"),
        ]
        indexes = [models.Index(fields=["target_table", "target_id"], name="core_legacy_map_target_idx")]

    def __str__(self):
        return f"{self.source_system}.{self.source_table}#{self.source_id} → {self.target_table}#{self.target_id}"
