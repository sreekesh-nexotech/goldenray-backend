"""Machine credentials for office agents (PLAN §1.4 "Auth (machines)").

Token format ``fl_<prefix>_<secret>``: ``prefix`` (12 hex chars) is stored in clear and indexed for lookup; only
``sha256(token)`` is stored. The plaintext is returned exactly once, by :func:`issue` / :func:`rotate`.
Verification always hashes and compares with ``hmac.compare_digest`` — also when the prefix is unknown — so the
response time does not reveal which prefixes exist.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import authentication, exceptions

from core.errors import Conflict, DomainError
from core.models import ServiceCredential
from core.services.stamping import stamp_create
from core.services.versioning import check_version

TOKEN_PREFIX = "fl"
PREFIX_BYTES = 6  # 12 hex characters
SECRET_BYTES = 32
LAST_USED_RESOLUTION = timedelta(minutes=1)
_DUMMY_HASH = hashlib.sha256(b"flarize-service-credential-dummy").hexdigest()


def _audit(action: str, credential: ServiceCredential, user) -> None:
    """Audit row for a credential change. Only the public prefix is recorded — never the token or its hash."""
    from audit.services import record

    record(action, obj=credential, actor=user, actor_kind=None if user is not None else "SYSTEM", after={"kind": credential.kind, "name": credential.name, "prefix": credential.token_prefix})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> tuple[str, str]:
    prefix = secrets.token_hex(PREFIX_BYTES)
    return prefix, f"{TOKEN_PREFIX}_{prefix}_{secrets.token_urlsafe(SECRET_BYTES)}"


def parse_token(token: str) -> str | None:
    """Return the prefix of a well-formed token, else ``None``."""
    parts = (token or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or len(parts[1]) != PREFIX_BYTES * 2 or not parts[2]:
        return None
    try:
        int(parts[1], 16)
    except ValueError:
        return None
    return parts[1]


@transaction.atomic
def issue(kind: str, name: str, bound_object=None, *, user=None) -> tuple[ServiceCredential, str]:
    """Create a credential. Returns ``(credential, plaintext_token)``; the plaintext is never stored."""
    if kind not in ServiceCredential.Kind.values:
        raise DomainError("invalid_credential_kind", f"Unknown credential kind {kind!r}.")
    if not name or len(name) > 120:
        raise DomainError("validation_error", "name is required (max 120 characters).", errors={"name": ["Required, max 120 characters."]})
    for _ in range(5):
        prefix, token = _new_token()
        credential = ServiceCredential(kind=kind, name=name, token_prefix=prefix, token_hash=hash_token(token), issued_at=timezone.now())
        if bound_object is not None:
            credential.bound_content_type = ContentType.objects.get_for_model(bound_object)
            credential.bound_object_id = bound_object.pk
        stamp_create(credential, user)
        try:
            with transaction.atomic():
                credential.save()
        except IntegrityError:
            continue  # prefix collision (2^48 space) — draw again
        _audit("core.service_credential_issued", credential, user)
        return credential, token
    raise Conflict("credential_prefix_exhausted", "Could not allocate a unique token prefix; try again.")


@transaction.atomic
def rotate(credential: ServiceCredential, *, user=None, expected_version=None) -> tuple[ServiceCredential, str]:
    """Replace the token (old token stops working immediately). Returns ``(credential, new_plaintext_token)``."""
    credential = ServiceCredential.objects.select_for_update().get(pk=credential.pk)
    check_version(credential, expected_version)
    if credential.is_revoked:
        raise Conflict("credential_revoked", "A revoked credential cannot be rotated; issue a new one.")
    for _ in range(5):
        prefix, token = _new_token()
        try:
            with transaction.atomic():
                credential.versioned_update(user, token_prefix=prefix, token_hash=hash_token(token), issued_at=timezone.now(), last_used_at=None)
        except IntegrityError:
            continue
        _audit("core.service_credential_rotated", credential, user)
        return credential, token
    raise Conflict("credential_prefix_exhausted", "Could not allocate a unique token prefix; try again.")


@transaction.atomic
def revoke(credential: ServiceCredential, *, user=None, expected_version=None) -> ServiceCredential:
    credential = ServiceCredential.objects.select_for_update().get(pk=credential.pk)
    check_version(credential, expected_version)
    if credential.is_revoked:
        return credential
    credential.versioned_update(user, revoked_at=timezone.now())
    _audit("core.service_credential_revoked", credential, user)
    return credential


def verify(token: str) -> ServiceCredential | None:
    """The live, unrevoked credential matching ``token`` or ``None`` (constant-time comparison on every path)."""
    prefix = parse_token(token)
    credential = ServiceCredential.objects.filter(token_prefix=prefix, revoked_at__isnull=True).first() if prefix else None
    expected = credential.token_hash if credential is not None else _DUMMY_HASH
    matches = hmac.compare_digest(hash_token(token or ""), expected)
    return credential if credential is not None and matches else None


def touch_last_used(credential: ServiceCredential) -> None:
    """Record use at most once per minute (telemetry: no version bump, no attribution change)."""
    now = timezone.now()
    if credential.last_used_at is None or now - credential.last_used_at >= LAST_USED_RESOLUTION:
        ServiceCredential.all_objects.filter(pk=credential.pk).update(last_used_at=now)
        credential.last_used_at = now


@dataclass(frozen=True)
class ServicePrincipal:
    """``request.user`` for machine callers. Never an ``accounts.User``: staff permissions never apply to it."""

    credential: ServiceCredential

    is_authenticated = True
    is_anonymous = False
    is_active = True

    @property
    def pk(self) -> str:
        return f"svc:{self.credential.uid}"

    @property
    def uid(self):
        return self.credential.uid

    @property
    def kind(self) -> str:
        return self.credential.kind

    @property
    def bound_object(self):
        return self.credential.bound_object

    def __str__(self) -> str:
        return f"{self.credential.kind}:{self.credential.name}"


class ServiceTokenAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer fl_<prefix>_<secret>`` for ``/api/agent/<version>/`` views."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid Authorization header.")
        try:
            token = header[1].decode("ascii")
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Invalid Authorization header.") from None
        credential = verify(token)
        if credential is None:
            raise exceptions.AuthenticationFailed("Invalid or revoked service token.")
        touch_last_used(credential)
        principal = ServicePrincipal(credential)
        from audit import context as audit_context

        audit_context.set_actor(principal, credential.kind)
        return principal, credential

    def authenticate_header(self, request):
        return f'{self.keyword} realm="api"'
