"""Password policy, hashing and single-use reset tokens.

* **One path sets a password:** :func:`apply_new_password` validates against ``AUTH_PASSWORD_VALIDATORS`` (with the
  user, so similarity to their name/e-mail is caught) before hashing — change, reset and every future path call
  it. ``UserManager.create_user`` validates too. There are no default passwords anywhere: new accounts get an
  unusable password and a set-password link.
* :func:`verify_password` **always** runs the password hasher — against a dummy Argon2 hash when the user is
  unknown or has no usable password — so response time does not reveal whether an account exists or is locked.
* Reset tokens are 256-bit random strings; only their sha256 is stored (``accounts_password_reset.token_hash``).
  They are single use and expire (``ACCOUNTS_PASSWORD_RESET_TTL``, invites ``ACCOUNTS_INVITE_TTL``).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from django.utils import timezone

from accounts.models import PasswordReset
from core.errors import DomainError
from core.models import actor_or_none
from core.services import stamp_create

MAX_PASSWORD_LENGTH = 1024
TOKEN_BYTES = 32


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A hash made by the current default hasher, so the dummy verification costs what a real one does."""
    return make_password(secrets.token_urlsafe(24))


def verify_password(user, raw_password: str) -> bool:
    """Check ``raw_password`` for ``user`` (which may be ``None``); the hasher runs on every path.

    A correct password stored with an older hasher (PBKDF2 from the CMS, bcrypt from eSSL) is upgraded to Argon2.
    """
    raw_password = raw_password or ""
    if user is not None and user.has_usable_password():
        return user.check_password(raw_password)
    check_password(raw_password, _dummy_hash())
    return False


def validate_new_password(raw_password: str, user=None, *, field: str = "new_password") -> None:
    """Raise ``DomainError(validation_error)`` with ``{field: [messages]}`` when the policy rejects the password."""
    if not isinstance(raw_password, str) or not raw_password:
        raise DomainError("validation_error", "Invalid input.", errors={field: ["A password is required."]})
    if len(raw_password) > MAX_PASSWORD_LENGTH:
        raise DomainError("validation_error", "Invalid input.", errors={field: [f"Use at most {MAX_PASSWORD_LENGTH} characters."]})
    try:
        password_validation.validate_password(raw_password, user)
    except DjangoValidationError as exc:
        raise DomainError("validation_error", "Invalid input.", errors={field: list(exc.messages)}) from None


def apply_new_password(user, raw_password: str, *, actor, field: str = "new_password") -> None:
    """Validate, hash and store a new password (compare-and-swap on ``version``); clears ``must_reset_password``."""
    validate_new_password(raw_password, user, field=field)
    user.versioned_update(actor, password=make_password(raw_password), password_changed_at=timezone.now(), must_reset_password=False)
    password_validation.password_changed(raw_password, user)


# ----------------------------------------------------------------------------------------------------------------------
# Reset tokens
# ----------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class IssuedReset:
    reset: PasswordReset
    token: str
    link: str


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def reset_link(token: str) -> str:
    """The Studio page completing the reset. The token rides in the fragment, which browsers never send or log."""
    return f"{settings.ACCOUNTS_PASSWORD_RESET_URL}#token={token}"


def issue_reset(user, *, actor=None, ttl: timedelta | None = None) -> IssuedReset:
    """Create a single-use reset token for ``user`` (caller holds the transaction). The plaintext is returned once."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    reset = PasswordReset(user=user, token_hash=hash_token(token), expires_at=timezone.now() + (ttl or settings.ACCOUNTS_PASSWORD_RESET_TTL))
    stamp_create(reset, actor_or_none(actor))
    reset.save()
    return IssuedReset(reset=reset, token=token, link=reset_link(token))


def void_open_resets(user, *, actor=None) -> int:
    """Consume every unused, unexpired reset token of ``user`` (a newer one supersedes them)."""
    now = timezone.now()
    return PasswordReset.objects.filter(user=user, used_at__isnull=True, expires_at__gt=now).update(used_at=now, updated_at=now, updated_by=actor_or_none(actor), version=F("version") + 1)


def recent_reset_count(user, *, within: timedelta = timedelta(hours=1)) -> int:
    return PasswordReset.all_objects.filter(user=user, created_at__gte=timezone.now() - within).count()
