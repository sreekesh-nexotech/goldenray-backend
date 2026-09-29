"""``leads_otp_request`` (PLAN §2.6, no base): one row per code sent through Twilio Verify.

The code itself is never stored (Twilio Verify generates and checks it). ``attempts`` counts our verification
checks against the row (capped), ``verified_at`` is set once Twilio approves, ``expires_at`` bounds both.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone


class OtpRequest(models.Model):
    class Purpose(models.TextChoices):
        LEAD = "LEAD", "Website enquiry"
        APPROVAL = "APPROVAL", "Customer approval"

    id = models.BigAutoField(primary_key=True)
    phone_e164 = models.CharField(max_length=16)
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.LEAD)
    provider_sid = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "leads_otp_request"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(purpose__in=["LEAD", "APPROVAL"]), name="leads_otp_request_purpose_valid"),
            models.CheckConstraint(condition=Q(phone_e164__regex=r"^\+[1-9][0-9]{7,14}$"), name="leads_otp_request_phone_e164_format"),
            models.CheckConstraint(condition=Q(expires_at__gt=models.F("created_at")), name="leads_otp_request_expires_after_created"),
        ]
        indexes = [models.Index(fields=["phone_e164", "created_at"], name="leads_otp_request_phone_at")]

    def __str__(self) -> str:
        return f"OTP {self.purpose} {self.phone_e164} @ {self.created_at:%Y-%m-%d %H:%M}"
