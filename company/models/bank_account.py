"""``company_bank_account`` (PLAN §2.8): accounts printed on quotations/agreements; one primary."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel
from flarize.crypto import EncryptedTextField


class BankAccount(BaseModel):
    label = models.CharField(max_length=80)
    bank = models.CharField(max_length=120)
    account_name = models.CharField(max_length=160)
    account_number = EncryptedTextField()  # Fernet; never returned in full by the API
    ifsc = models.CharField(max_length=11)
    branch = models.CharField(max_length=160, blank=True, default="")
    upi_id = models.CharField(max_length=100, blank=True, default="")
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "company_bank_account"
        ordering = ["-is_primary", "label", "id"]
        constraints = [
            models.UniqueConstraint(fields=["is_primary"], condition=Q(is_primary=True, deleted_at__isnull=True), name="company_bank_account_one_primary"),
            models.UniqueConstraint(fields=["label"], condition=Q(deleted_at__isnull=True), name="company_bank_account_label_live_uniq"),
            models.CheckConstraint(condition=Q(ifsc__regex=r"^[A-Z]{4}0[A-Z0-9]{6}$"), name="company_bank_account_ifsc_format"),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.bank})"
