"""Retention of authentication records (run daily by ``accounts.tasks.purge_auth_records``).

* ``accounts_login_attempt`` rows older than ``ACCOUNTS_LOGIN_ATTEMPT_RETENTION`` (the lockout only looks back
  ``ACCOUNTS_LOGIN_LOCKOUT_WINDOW``; the audit log keeps the long-term trail);
* SimpleJWT outstanding tokens past their expiry (their blacklist rows cascade) — an expired token is rejected on
  its ``exp`` alone;
* password-reset rows that expired more than 30 days ago (used or not).

Sessions are kept: they are small and form the sign-in history shown to users.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from accounts.models import PasswordReset
from accounts.services import lockout

RESET_RETENTION = timedelta(days=30)


@transaction.atomic
def purge(*, now: datetime, attempt_retention: timedelta) -> dict:
    attempts = lockout.purge_before(now - attempt_retention)
    tokens, _ = OutstandingToken.objects.filter(expires_at__lt=now).delete()
    resets, _ = PasswordReset.all_objects.filter(expires_at__lt=now - RESET_RETENTION).delete()
    return {"login_attempts": attempts, "tokens": tokens, "password_resets": resets}
