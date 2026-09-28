from django.conf import settings
from django.db import models

from core.models import BaseModel


class UserSession(BaseModel):
    """One row per refresh-token family (login). Revoking it ends the session on its next refresh."""

    # True child of the user: CASCADE.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions")
    refresh_jti = models.UUIDField(unique=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_user_session"
        indexes = [models.Index(fields=["user", "expires_at"], name="accounts_session_user_exp_idx")]

    def __str__(self):
        return f"session {self.uid} of user {self.user_id}"
