"""``company_integration`` (PLAN §2.8, §5.3): provider settings editable by Admin; the whole config is one Fernet token."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel
from flarize.crypto import EncryptedJSONField


class Integration(BaseModel):
    class Key(models.TextChoices):
        TWILIO = "TWILIO", "Twilio Verify"
        BUNNY = "BUNNY", "Bunny storage / CDN"
        SMTP = "SMTP", "SMTP e-mail"

    key = models.CharField(max_length=16, choices=Key.choices)
    config = EncryptedJSONField(default=dict)  # Fernet-encrypted JSON; secrets are write-only in the API
    is_enabled = models.BooleanField(default=False)

    class Meta:
        db_table = "company_integration"
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["key"], condition=Q(deleted_at__isnull=True), name="company_integration_key_live_uniq"),
            models.CheckConstraint(condition=Q(key__in=["TWILIO", "BUNNY", "SMTP"]), name="company_integration_key_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.key} ({'enabled' if self.is_enabled else 'disabled'})"
