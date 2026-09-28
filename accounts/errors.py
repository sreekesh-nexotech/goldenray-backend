"""Domain errors raised by the accounts services (rendered by ``flarize.exceptions`` as the standard envelope).

Codes are stable strings the Studio BFF branches on:

* ``invalid_credentials`` (401) — wrong e-mail/password, unknown or inactive user (indistinguishable on purpose);
* ``login_locked`` (429, ``Retry-After``) — too many failures for this e-mail or client IP;
* ``password_reset_required`` (403) — correct password but the account must set a new one via the reset link;
* ``token_invalid`` / ``session_revoked`` / ``session_expired`` / ``refresh_token_rotated`` /
  ``refresh_token_reused`` / ``user_inactive`` (401) — refresh-token failures;
* ``reset_token_invalid`` / ``reset_token_expired`` (400) — password-reset token failures.
"""

from __future__ import annotations

import math

from core.errors import DomainError


class AuthError(DomainError):
    status = 401
    default_code = "not_authenticated"
    default_message = "Authentication failed."


class InvalidCredentials(AuthError):
    default_code = "invalid_credentials"
    default_message = "The e-mail address or password is incorrect."


class TokenRejected(AuthError):
    default_code = "token_invalid"
    default_message = "The token is invalid or has expired."


class LoginLocked(DomainError):
    status = 429
    default_code = "login_locked"
    default_message = "Too many failed sign-in attempts. Try again later."

    def __init__(self, retry_after_seconds: float, **kwargs):
        super().__init__(**kwargs)
        self.retry_after = max(1, math.ceil(retry_after_seconds))


class PasswordResetRequired(DomainError):
    status = 403
    default_code = "password_reset_required"
    default_message = "This account must set a new password. Use the reset link sent by e-mail, or request a new one."
