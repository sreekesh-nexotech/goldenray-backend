from django.conf import settings
from django.db import models

from core.models import BaseModel


class PasswordReset(BaseModel):
    """A single-use reset token; only its sha256 is stored."""

    # True child of the user: CASCADE.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_resets")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_password_reset"
        indexes = [models.Index(fields=["user", "expires_at"], name="accounts_pwreset_user_exp_idx")]

    def __str__(self):
        return f"password reset {self.uid} for user {self.user_id}"
