from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q

from core.models.base import BaseModel


class ServiceCredential(BaseModel):
    """A machine credential (office agent). Only the sha256 of the token is stored; lookup is by prefix."""

    class Kind(models.TextChoices):
        AGENT = "AGENT", "Office agent"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=120)
    token_prefix = models.CharField(max_length=16, db_index=True)
    token_hash = models.CharField(max_length=64)
    issued_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Lookup table: PROTECT (content types are never deleted while a credential points at them).
    bound_content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    bound_object_id = models.BigIntegerField(null=True, blank=True)
    bound_object = GenericForeignKey("bound_content_type", "bound_object_id")

    class Meta:
        db_table = "core_service_credential"
        constraints = [
            models.UniqueConstraint(fields=["token_prefix"], condition=Q(deleted_at__isnull=True), name="core_service_credential_prefix_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["AGENT"]), name="core_service_credential_kind_valid"),
        ]
        indexes = [models.Index(fields=["bound_content_type", "bound_object_id"], name="core_svc_cred_bound_idx")]

    def __str__(self):
        return f"{self.kind}:{self.name}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None
