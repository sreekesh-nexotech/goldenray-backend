"""DB-counted login lockout (standard §3.1): ``ACCOUNTS_LOGIN_MAX_FAILURES_PER_EMAIL`` failures per e-mail and
``ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP`` failures per client IP within ``ACCOUNTS_LOGIN_LOCKOUT_WINDOW``.

* Counting lives in ``accounts_login_attempt`` (not Redis), so a cache outage cannot switch the lockout off.
* A successful sign-in clears the e-mail's count (failures before it no longer matter); it never clears an IP's
  count, so an attacker spraying passwords cannot reset their budget by signing in to their own account.
* Attempts refused *because* of a lockout are not recorded as failures (they are audited), so a lockout expires
  ``window`` after the failure that triggered it instead of being extended forever by an attacker.
* Unknown e-mails are counted like known ones: a lockout reveals nothing about whether an account exists.
* The check and the counting are one step (:func:`reserve`): under transaction-scoped advisory locks per e-mail and
  per client IP, an attempt that passes the check is recorded as a failure *before* the password is verified, and
  flipped to a success (:func:`succeed`) once it proves right. Concurrent attempts therefore cannot all pass the
  check while each spends its Argon2 time verifying; the slow hash never runs inside the lock.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from accounts.models import LoginAttempt


@dataclass(frozen=True)
class LockState:
    locked: bool
    retry_after: float = 0.0
    reason: str = ""  # "email" | "ip"


UNLOCKED = LockState(False)


def email_key(email: str | None) -> str:
    """The form an e-mail is counted under (``accounts_login_attempt.email``)."""
    return (email or "").strip().lower()[:254]


def _state(queryset, *, limit: int, now: datetime, reset_on_success: bool, reason: str) -> LockState:
    window = settings.ACCOUNTS_LOGIN_LOCKOUT_WINDOW
    recent = queryset.filter(at__gte=now - window)
    if reset_on_success:
        last_success = recent.filter(succeeded=True).aggregate(last=Max("at"))["last"]
        if last_success is not None:
            recent = recent.filter(at__gt=last_success)
    failures = list(recent.filter(succeeded=False).order_by("-at").values_list("at", flat=True)[:limit])
    if limit <= 0 or len(failures) < limit:
        return UNLOCKED
    # Locked until the oldest of the `limit` most recent failures leaves the window.
    return LockState(True, max(1.0, (failures[-1] + window - now).total_seconds()), reason)


def check(email: str, ip: str | None, *, now: datetime | None = None) -> LockState:
    """Whether a sign-in for ``email`` from ``ip`` is locked out right now (the longer wait wins)."""
    now = now or timezone.now()
    states = [_state(LoginAttempt.objects.filter(email=email_key(email)), limit=settings.ACCOUNTS_LOGIN_MAX_FAILURES_PER_EMAIL, now=now, reset_on_success=True, reason="email")]
    if ip:
        states.append(_state(LoginAttempt.objects.filter(ip=ip), limit=settings.ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP, now=now, reset_on_success=False, reason="ip"))
    locked = [state for state in states if state.locked]
    return max(locked, key=lambda state: state.retry_after) if locked else UNLOCKED


def record_attempt(email: str, ip: str | None, *, succeeded: bool) -> LoginAttempt:
    return LoginAttempt.objects.create(email=email_key(email), ip=ip or None, succeeded=succeeded)


@dataclass(frozen=True)
class Reservation:
    """Outcome of :func:`reserve`: the lock state, and the attempt counted as a failure when not locked."""

    state: LockState
    attempt: LoginAttempt | None = None

    @property
    def locked(self) -> bool:
        return self.state.locked


def _advisory_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


def _serialise(key: str, ip: str | None) -> None:
    """Hold the transaction-scoped advisory locks of this e-mail and client IP (always in that order: no deadlock)."""
    names = [f"accounts.login.email:{key}"] + ([f"accounts.login.ip:{ip}"] if ip else [])
    with connection.cursor() as cursor:
        for name in names:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_advisory_key(name)])


def reserve(email: str, ip: str | None, *, now: datetime | None = None) -> Reservation:
    """Check the lockout and, unless locked, count this attempt as a failure — atomically (see module docstring).

    The caller verifies the password afterwards and calls :func:`succeed` when it was right. A refused (locked)
    attempt is not counted.
    """
    key = email_key(email)
    with transaction.atomic():
        _serialise(key, ip or None)
        state = check(key, ip, now=now)
        if state.locked:
            return Reservation(state)
        return Reservation(UNLOCKED, record_attempt(key, ip, succeeded=False))


def succeed(reservation: Reservation) -> None:
    """Turn a reserved attempt into a success (it then clears the e-mail's count; an IP's count never resets)."""
    if reservation.attempt is not None:
        LoginAttempt.objects.filter(pk=reservation.attempt.pk).update(succeeded=True)


def purge_before(cutoff: datetime) -> int:
    """Delete attempts older than ``cutoff`` (retention job); returns the number deleted."""
    deleted, _ = LoginAttempt.objects.filter(at__lt=cutoff).delete()
    return deleted
