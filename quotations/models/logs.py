"""``quotations_email_log`` *(no base)* — every quotation sent to a customer (replaces the website's ``sent_quotes``)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from quotations.models.choices import EmailChannel, EmailStatus, Language, in_choices


class EmailLog(models.Model):
    """Append-only send log. ``version`` is NULL for imported legacy rows (``sent_quotes`` had no quotation)."""

    # CASCADE: a log row is a child of the version it sent (versions are never hard-deleted).
    version = models.ForeignKey("quotations.Version", null=True, blank=True, on_delete=models.CASCADE, related_name="email_logs")
    channel = models.CharField(max_length=12, choices=EmailChannel.choices, default=EmailChannel.EMAIL)
    to = models.CharField(max_length=254, help_text="Recipient e-mail address (legacy rows: the phone number).")
    name = models.CharField(max_length=100, blank=True, default="")
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    status = models.CharField(max_length=8, choices=EmailStatus.choices, default=EmailStatus.QUEUED)
    provider_id = models.CharField(max_length=128, blank=True, default="")
    error = models.CharField(max_length=500, blank=True, default="")
    legacy_ref = models.CharField(max_length=64, blank=True, default="", help_text="sent_quotes.quote_id of an imported row.")
    legacy_url = models.URLField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "quotations_email_log"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["legacy_ref"], condition=~Q(legacy_ref=""), name="quotations_email_log_legacy_ref_uniq"),
            models.CheckConstraint(condition=in_choices("channel", EmailChannel), name="quotations_email_log_channel_valid"),
            models.CheckConstraint(condition=in_choices("status", EmailStatus), name="quotations_email_log_status_valid"),
            models.CheckConstraint(condition=in_choices("language", Language), name="quotations_email_log_language_valid"),
            models.CheckConstraint(condition=Q(version__isnull=False) | ~Q(legacy_ref=""), name="quotations_email_log_version_or_legacy"),
            models.CheckConstraint(condition=~Q(status="SENT") | Q(sent_at__isnull=False), name="quotations_email_log_sent_has_at"),
        ]
        indexes = [models.Index(fields=["version", "-created_at"], name="quotations_email_log_version")]

    def __str__(self) -> str:
        return f"{self.channel} {self.status} {self.created_at:%Y-%m-%d}"
