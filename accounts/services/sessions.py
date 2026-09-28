"""Staff sessions: one ``UserSession`` per login (a refresh-token family).

Tokens (SimpleJWT, RS256) carry identity only: ``sub`` (user uid), ``sid`` (session uid), ``jti`` plus the standard
``token_type``/``exp``/``iat``/``iss``. Nothing about roles or permissions is ever put in a token.

* The session row records the **current** refresh ``jti``; a refresh rotates it (``accounts.services.auth.refresh``).
* Every access token names its session; ``SessionAwareJWTAuthentication`` refuses the token as soon as the session is
  revoked or expired. Liveness is cached for ``ACCOUNTS_SESSION_LIVENESS_CACHE_SECONDS`` under the version namespaces
  ``accounts:session:<sid>`` and ``accounts:user:<uid>``; every revocation bumps them, so it is effective on the very
  next request.
* A session slides with each refresh (a new 7-day refresh token) but never outlives ``ACCOUNTS_SESSION_MAX_AGE``
  from login.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch, datetime_to_epoch

from accounts.models import UserSession
from accounts.services.authz import user_namespace
from audit.services import record
from core.errors import NotFound
from core.models import actor_or_none
from core.services import stamp_create
from flarize.cache_utils import build_key, bump, get_versions

logger = logging.getLogger("flarize.sessions")

SESSION_CLAIM = "sid"


@dataclass(frozen=True)
class IssuedTokens:
    access: str
    refresh: str
    session: UserSession
    access_expires_at: datetime
    refresh_expires_at: datetime


def session_namespace(session_uid) -> str:
    return f"accounts:session:{session_uid}"


def session_expiry(created_at: datetime, now: datetime) -> datetime:
    """Refresh/session expiry: ``now + REFRESH_TOKEN_LIFETIME``, capped at ``created_at + ACCOUNTS_SESSION_MAX_AGE``."""
    return min(now + api_settings.REFRESH_TOKEN_LIFETIME, created_at + settings.ACCOUNTS_SESSION_MAX_AGE)


def build_tokens(user, session: UserSession, expires_at: datetime) -> tuple[RefreshToken, AccessToken]:
    """A new refresh token for ``session`` (expiring at ``expires_at``) and its access token (never outliving it)."""
    refresh = RefreshToken()
    refresh[api_settings.USER_ID_CLAIM] = str(user.uid)
    refresh[SESSION_CLAIM] = str(session.uid)
    refresh["exp"] = datetime_to_epoch(expires_at)
    access = refresh.access_token
    if access["exp"] > refresh["exp"]:
        access["exp"] = refresh["exp"]
    return refresh, access


def issued(refresh: RefreshToken, access: AccessToken, session: UserSession) -> IssuedTokens:
    return IssuedTokens(
        access=str(access),
        refresh=str(refresh),
        session=session,
        access_expires_at=datetime_from_epoch(access["exp"]),
        refresh_expires_at=datetime_from_epoch(refresh["exp"]),
    )


@transaction.atomic
def start_session(user, *, ip: str | None = None, user_agent: str = "") -> IssuedTokens:
    """Create a session for ``user`` and issue its first token pair (the login service calls this after the checks)."""
    now = timezone.now()
    session = UserSession(user=user, ip=ip or None, user_agent=(user_agent or "")[:512], created_at=now)
    expires_at = session_expiry(now, now)
    refresh, access = build_tokens(user, session, expires_at)
    session.refresh_jti = uuid.UUID(refresh[api_settings.JTI_CLAIM])
    session.expires_at = expires_at
    stamp_create(session, user)
    session.save()
    refresh.outstand()
    return issued(refresh, access, session)


def is_session_live(user, session_uid) -> bool:
    """Whether ``session_uid`` is an unrevoked, unexpired session of ``user`` (cached briefly, version-keyed)."""
    try:
        sid = uuid.UUID(str(session_uid))
    except (TypeError, ValueError):
        return False
    key = build_key("session", str(sid), str(user.uid), versions=get_versions([session_namespace(sid), user_namespace(user.uid)]))
    now = timezone.now().timestamp()
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 - cache outage: ask the database
        logger.warning("session liveness cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return float(cached) > now
    expires_at = UserSession.objects.filter(uid=sid, user_id=user.pk, revoked_at__isnull=True).values_list("expires_at", flat=True).first()
    expires = expires_at.timestamp() if expires_at else 0.0
    try:
        cache.set(key, expires, int(settings.ACCOUNTS_SESSION_LIVENESS_CACHE_SECONDS))
    except Exception:  # noqa: BLE001
        logger.warning("session liveness cache write failed", exc_info=True)
    return expires > now


def blacklist_jtis(jtis: Iterable) -> None:
    """Blacklist the refresh tokens with these ``jti`` values (defence in depth next to session revocation)."""
    hexes = [uuid.UUID(str(jti)).hex for jti in jtis if jti]
    if not hexes:
        return
    tokens = OutstandingToken.objects.filter(jti__in=hexes)
    BlacklistedToken.objects.bulk_create([BlacklistedToken(token=token) for token in tokens], ignore_conflicts=True)


def live_sessions(user):
    """The user's unrevoked, unexpired sessions, newest first."""
    return UserSession.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at", "-id")


@transaction.atomic
def revoke_session(session: UserSession, *, user, reason: str, action: str = "accounts.session_revoked") -> bool:
    """End one session (idempotent). Returns ``False`` when it had already ended."""
    locked = UserSession.all_objects.select_for_update(of=("self",)).select_related("user").get(pk=session.pk)
    if locked.revoked_at is not None:
        return False
    locked.versioned_update(user, revoked_at=timezone.now())
    blacklist_jtis([locked.refresh_jti])
    bump(session_namespace(locked.uid))
    record(action, obj=locked, actor=user, actor_kind=None if actor_or_none(user) else "SYSTEM", after={"user_uid": str(locked.user.uid), "reason": reason})
    session.revoked_at, session.version = locked.revoked_at, locked.version
    return True


@transaction.atomic
def revoke_all_sessions(target, *, user, reason: str, keep_session_uid=None) -> int:
    """End every live session of ``target`` (optionally keeping one, e.g. the caller's current session)."""
    now = timezone.now()
    sessions = UserSession.objects.select_for_update().filter(user=target, revoked_at__isnull=True)
    if keep_session_uid is not None:
        sessions = sessions.exclude(uid=keep_session_uid)
    rows = list(sessions.values_list("pk", "uid", "refresh_jti"))
    if not rows:
        return 0
    actor = actor_or_none(user)
    UserSession.all_objects.filter(pk__in=[pk for pk, _, _ in rows]).update(revoked_at=now, updated_at=now, updated_by=actor, version=F("version") + 1)
    blacklist_jtis([jti for _, _, jti in rows])
    namespaces = [session_namespace(uid) for _, uid, _ in rows]
    if keep_session_uid is None:
        namespaces.append(user_namespace(target.uid))
    bump(*namespaces)
    record("accounts.sessions_revoked", obj=target, actor=user, actor_kind=None if actor else "SYSTEM", after={"count": len(rows), "reason": reason})
    return len(rows)


def get_own_session(user, session_uid) -> UserSession:
    session = live_sessions(user).filter(uid=session_uid).first()
    if session is None:
        raise NotFound("session_not_found", "No active session with this id.")
    return session
