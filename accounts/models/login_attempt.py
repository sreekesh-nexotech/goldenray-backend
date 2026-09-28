from django.db import models
from django.utils import timezone


class LoginAttempt(models.Model):
    """Every login attempt (no base). Drives the 5-per-15-minutes lockout.

    Rows are inserted as failures *before* the password is verified (``accounts.services.lockout.reserve``); the only
    update ever made is flipping ``succeeded`` once the password proved right. Rows are never deleted except by the
    retention job.
    """

    id = models.BigAutoField(primary_key=True)
    email = models.CharField(max_length=254)
    ip = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_login_attempt"
        indexes = [
            models.Index(fields=["email", "at"], name="accounts_login_email_at_idx"),
            models.Index(fields=["ip", "at"], name="accounts_login_ip_at_idx"),
        ]

    def __str__(self):
        return f"{self.email} {'ok' if self.succeeded else 'failed'} at {self.at:%Y-%m-%d %H:%M:%S}"
