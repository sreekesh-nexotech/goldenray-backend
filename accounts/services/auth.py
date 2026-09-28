"""Staff authentication flows: login, refresh, logout, password change and password reset.

Failure paths that must leave a trace (a failed login counts towards the lockout; a replayed refresh token ends the
session) write inside their own transaction and raise only **after** it committed — the error then reaches the
client without rolling the record back. Every flow writes an audit row (``accounts.*``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import ExpiredTokenError, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken, Token

from accounts.errors import InvalidCredentials, LoginLocked, PasswordResetRequired, TokenRejected
from accounts.models import PasswordReset, User, UserSession
from accounts.services import emails, lockout, passwords, sessions
from audit.services import record
from core.errors import DomainError
from flarize.cache_utils import bump

logger = logging.getLogger("flarize.auth")

USER_OBJECT_TYPE = "accounts.user"
MAX_TOKEN_LENGTH = 4096


# ----------------------------------------------------------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------------------------------------------------------
def _find_user(email: str) -> User | None:
    email = (email or "").strip()
    if not email:
        return None
    return User.objects.select_related("role").filter(email=email).first()


def login(email: str, password: str, *, ip: str | None = None, user_agent: str = "") -> sessions.IssuedTokens:
    """Verify credentials and open a session. Raises ``LoginLocked``, ``InvalidCredentials`` or ``PasswordResetRequired``.

    The password hasher runs on every path (unknown e-mail, inactive user, locked out) so timing reveals nothing.
    """
    key = lockout.email_key(email)
    lock = lockout.check(key, ip)
    user = _find_user(email)
    password_ok = passwords.verify_password(user, password)

    if lock.locked:
        with transaction.atomic():
            record("accounts.login_locked", object_type=USER_OBJECT_TYPE, object_uid=user.uid if user else None, actor_kind="USER", after={"email": key, "locked_by": lock.reason})
        raise LoginLocked(lock.retry_after)

    if user is None or not password_ok or not user.is_active:
        reason = "unknown_email" if user is None else ("wrong_password" if not password_ok else "inactive")
        with transaction.atomic():
            lockout.record_attempt(key, ip, succeeded=False)
            record("accounts.login_failed", object_type=USER_OBJECT_TYPE, object_uid=user.uid if user else None, actor_kind="USER", after={"email": key, "reason": reason})
        raise InvalidCredentials()

    if user.must_reset_password:
        with transaction.atomic():
            lockout.record_attempt(key, ip, succeeded=True)
            record("accounts.login_blocked", obj=user, actor_kind="USER", after={"reason": "password_reset_required"})
        raise PasswordResetRequired()

    with transaction.atomic():
        lockout.record_attempt(key, ip, succeeded=True)
        now = timezone.now()
        # Telemetry column: no version bump, no attribution change.
        User.all_objects.filter(pk=user.pk).update(last_login_at=now)
        user.last_login_at = now
        tokens = sessions.start_session(user, ip=ip, user_agent=user_agent)
        record("accounts.login", obj=user, actor=user, after={"session_uid": str(tokens.session.uid)})
    return tokens


# ----------------------------------------------------------------------------------------------------------------------
# Refresh / logout
# ----------------------------------------------------------------------------------------------------------------------
class _SessionRefreshToken(RefreshToken):
    """A refresh token whose blacklist check is left to the session policy, so a replay can be recognised."""

    def verify(self) -> None:
        Token.verify(self)  # exp, jti and token type — without BlacklistMixin's check


@dataclass(frozen=True)
class _RefreshClaims:
    token: _SessionRefreshToken
    user_uid: str
    session_uid: uuid.UUID
    jti: str


def _decode_refresh(raw: str) -> _RefreshClaims:
    """Signature, issuer, expiry and type are verified by SimpleJWT; ``sub``/``sid``/``jti`` must be present."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TOKEN_LENGTH:
        raise TokenRejected()
    token = _SessionRefreshToken(raw)  # raises TokenError / ExpiredTokenError
    try:
        return _RefreshClaims(token, str(token[api_settings.USER_ID_CLAIM]), uuid.UUID(str(token[sessions.SESSION_CLAIM])), str(token[api_settings.JTI_CLAIM]))
    except (KeyError, ValueError):
        raise TokenRejected() from None


def _end(session: UserSession, reason: str) -> None:
    sessions.revoke_session(session, user=None, reason=reason, action="accounts.session_ended")


@transaction.atomic
def _rotate(claims: _RefreshClaims, ip: str | None, user_agent: str) -> sessions.IssuedTokens | DomainError:
    """Rotate within one transaction; returns the new tokens or the error to raise once the transaction committed."""
    now = timezone.now()
    session = UserSession.objects.select_for_update(of=("self",)).select_related("user", "user__role").filter(uid=claims.session_uid).first()
    if session is None or str(session.user.uid) != claims.user_uid:
        return TokenRejected()
    if session.revoked_at is not None:
        return TokenRejected("session_revoked", "This session has ended. Sign in again.")
    if session.expires_at <= now:
        return TokenRejected("session_expired", "This session has expired. Sign in again.")
    user = session.user

    if session.refresh_jti.hex != claims.jti:
        # A superseded refresh token. Within the grace window it is a racing client (two tabs, a retrying BFF);
        # after it, someone is replaying a token they should not have: end the session (standard §3.3).
        if (now - session.updated_at).total_seconds() <= settings.ACCOUNTS_REFRESH_REUSE_GRACE_SECONDS:
            return TokenRejected("refresh_token_rotated", "This refresh token was already used; use the latest one.")
        record("accounts.refresh_token_reused", obj=session, actor_kind="SYSTEM", after={"user_uid": str(user.uid), "presented_jti": claims.jti})
        _end(session, "refresh_token_reused")
        return TokenRejected("refresh_token_reused", "This refresh token was already used. The session has been ended for safety; sign in again.")
    if BlacklistedToken.objects.filter(token__jti=claims.jti).exists():
        return TokenRejected()

    if not user.is_active or user.deleted_at is not None:
        _end(session, "user_inactive")
        return TokenRejected("user_inactive", "This account is inactive.")
    if user.must_reset_password:
        _end(session, "password_reset_required")
        return PasswordResetRequired()

    expires_at = sessions.session_expiry(session.created_at, now)
    if expires_at <= now:
        _end(session, "session_max_age")
        return TokenRejected("session_expired", "This session has expired. Sign in again.")

    claims.token.blacklist()
    refresh, access = sessions.build_tokens(user, session, expires_at)
    session.versioned_update(
        user,
        refresh_jti=uuid.UUID(refresh[api_settings.JTI_CLAIM]),
        expires_at=expires_at,
        ip=ip or session.ip,
        user_agent=(user_agent or session.user_agent)[:512],
    )
    refresh.outstand()
    bump(sessions.session_namespace(session.uid))
    return sessions.issued(refresh, access, session)


def refresh(raw_refresh: str, *, ip: str | None = None, user_agent: str = "") -> sessions.IssuedTokens:
    """Rotate a refresh token: the old one is blacklisted and the session re-policed (revoked, expired, user inactive,
    ``must_reset_password``) — a refresh can never mint a token for a session that should have ended."""
    try:
        claims = _decode_refresh(raw_refresh)
    except TokenError:
        raise TokenRejected() from None
    result = _rotate(claims, ip, user_agent)
    if isinstance(result, DomainError):
        raise result
    return result


def logout(raw_refresh: str) -> None:
    """End the session the refresh token belongs to (idempotent; an expired token has nothing left to end)."""
    try:
        claims = _decode_refresh(raw_refresh)
    except ExpiredTokenError:
        return
    except TokenError:
        raise TokenRejected() from None
    session = UserSession.objects.select_related("user").filter(uid=claims.session_uid).first()
    if session is None or str(session.user.uid) != claims.user_uid:
        raise TokenRejected()
    with transaction.atomic():
        sessions.revoke_session(session, user=session.user, reason="logout", action="accounts.logout")
        claims.token.blacklist()


# ----------------------------------------------------------------------------------------------------------------------
# Password change / reset
# ----------------------------------------------------------------------------------------------------------------------
def change_password(user: User, *, current_password: str, new_password: str, session_uid=None, ip: str | None = None) -> int:
    """Change the caller's own password; ends every other session. Returns how many sessions were ended.

    A wrong current password counts towards the login lockout (a stolen access token must not become a password
    oracle).
    """
    key = lockout.email_key(user.email)
    lock = lockout.check(key, ip)
    current_ok = passwords.verify_password(user, current_password)
    if lock.locked:
        with transaction.atomic():
            record("accounts.password_change_locked", obj=user, actor=user, after={"locked_by": lock.reason})
        raise LoginLocked(lock.retry_after)
    if not current_ok:
        with transaction.atomic():
            lockout.record_attempt(key, ip, succeeded=False)
            record("accounts.password_change_failed", obj=user, actor=user, after={"reason": "wrong_current_password"})
        raise DomainError("invalid_current_password", "The current password is incorrect.", errors={"current_password": ["The current password is incorrect."]})
    if new_password == current_password:
        raise DomainError("validation_error", "Invalid input.", errors={"new_password": ["The new password must differ from the current one."]})

    with transaction.atomic():
        locked_user = User.objects.select_for_update(of=("self",)).get(pk=user.pk)
        passwords.apply_new_password(locked_user, new_password, actor=user)
        ended = sessions.revoke_all_sessions(locked_user, user=user, reason="password_changed", keep_session_uid=session_uid)
        record("accounts.password_changed", obj=locked_user, actor=user, after={"other_sessions_ended": ended})
        emails.send_password_changed(locked_user)
    user.password, user.version = locked_user.password, locked_user.version
    return ended


def request_password_reset(email: str, *, ip: str | None = None) -> None:
    """E-mail a reset link when ``email`` belongs to an active account. Always returns normally (no enumeration);
    at most ``ACCOUNTS_RESET_REQUESTS_PER_HOUR`` links per account per hour (no mail bombing)."""
    user = _find_user(email)
    if user is None or not user.is_active:
        logger.info("password reset requested for an unknown or inactive account")
        return
    with transaction.atomic():
        if passwords.recent_reset_count(user) >= settings.ACCOUNTS_RESET_REQUESTS_PER_HOUR:
            record("accounts.password_reset_throttled", obj=user, actor_kind="USER")
            return
        passwords.void_open_resets(user)
        issued = passwords.issue_reset(user, ttl=settings.ACCOUNTS_PASSWORD_RESET_TTL)
        record("accounts.password_reset_requested", obj=user, actor_kind="USER", after={"reset_uid": str(issued.reset.uid)})
        emails.send_password_reset(user, issued.link, settings.ACCOUNTS_PASSWORD_RESET_TTL)


@transaction.atomic
def reset_password(token: str, new_password: str) -> User:
    """Set a new password with a reset (or invite) token. Single use; ends every session of the account."""
    if not isinstance(token, str) or not token or len(token) > 256:
        raise DomainError("reset_token_invalid", "This reset link is invalid or has already been used.")
    reset = PasswordReset.objects.select_for_update(of=("self",)).select_related("user").filter(token_hash=passwords.hash_token(token)).first()
    if reset is None or reset.used_at is not None or not reset.user.is_active or reset.user.deleted_at is not None:
        raise DomainError("reset_token_invalid", "This reset link is invalid or has already been used.")
    if reset.expires_at <= timezone.now():
        raise DomainError("reset_token_expired", "This reset link has expired. Request a new one.")
    user = User.objects.select_for_update(of=("self",)).get(pk=reset.user_id)
    passwords.apply_new_password(user, new_password, actor=user)
    reset.versioned_update(user, used_at=timezone.now())
    passwords.void_open_resets(user, actor=user)
    sessions.revoke_all_sessions(user, user=user, reason="password_reset")
    record("accounts.password_reset", obj=user, actor=user, after={"reset_uid": str(reset.uid)})
    emails.send_password_changed(user)
    return user
