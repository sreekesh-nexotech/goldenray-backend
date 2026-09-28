"""Authorization data for a user: the grants of their role, normalised against the registry.

Resolved server-side on every request — never from the token (standard §3.1). Two layers of caching:

* a per-request memo on the user object;
* a cross-request cache keyed by the version namespaces ``accounts:user:<uid>`` and ``accounts:role:<uid>``
  (standard §7.1). The accounts services bump them on every user/role write, so a changed grant takes effect on the
  next request. The key also embeds the role row's ``version`` and ``updated_at`` (the row is loaded with the user
  anyway), so even a write that bypasses the services can never serve stale grants.

Also here: the Super Admin identity, the escalation guards used by the user and role services, and
``deny_self_action`` (PLAN §3.2: nobody performs ``attendance.edit`` / ``leave.approve`` on their own record).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from accounts.registry import SCOPE_ALL, SELF_ACTION_DENIED, normalise_permissions, normalise_scopes
from core.errors import PermissionDenied
from flarize.cache_utils import build_key, bump, get_versions

logger = logging.getLogger("flarize.authz")

SUPER_ADMIN_SLUG = "super-admin"
_MEMO_ATTR = "_flarize_grants"


def user_namespace(user_uid) -> str:
    return f"accounts:user:{user_uid}"


def role_namespace(role_uid) -> str:
    return f"accounts:role:{role_uid}"


@dataclass(frozen=True)
class Grants:
    permissions: dict[str, frozenset[str]] = field(default_factory=dict)
    scopes: dict[str, str] = field(default_factory=dict)

    def allows(self, module: str, action: str) -> bool:
        return action in self.permissions.get(module, frozenset())

    def scope(self, module: str) -> str | None:
        return self.scopes.get(module) if module in self.permissions else None

    def as_dict(self) -> dict:
        return {"permissions": {module: sorted(actions) for module, actions in self.permissions.items()}, "scopes": dict(self.scopes)}

    @classmethod
    def from_dict(cls, data: Mapping) -> Grants:
        permissions = normalise_permissions(data.get("permissions"))
        scopes = normalise_scopes(data.get("scopes"), permissions)
        return cls({module: frozenset(actions) for module, actions in permissions.items()}, scopes)


EMPTY_GRANTS = Grants()


def grants_for_role(role) -> Grants:
    if role is None or role.deleted_at is not None:
        return EMPTY_GRANTS
    permissions = normalise_permissions(role.permissions)
    scopes = normalise_scopes(role.scopes, permissions)
    return Grants({module: frozenset(actions) for module, actions in permissions.items()}, scopes)


def _is_live_staff_user(user) -> bool:
    from accounts.models import User

    return isinstance(user, User) and user.pk is not None and user.is_authenticated and user.is_active and user.deleted_at is None


def _cache_key(user, role) -> str:
    versions = get_versions([user_namespace(user.uid), role_namespace(role.uid)])
    stamp = role.updated_at.isoformat() if role.updated_at else ""
    return build_key("authz", str(user.uid), str(role.uid), role.version, stamp, versions=versions)


def _cached_grants(user, role) -> Grants:
    key = _cache_key(user, role)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 - cache outage: resolve from the row
        logger.warning("grants cache read failed", exc_info=True)
        cached = None
    if isinstance(cached, Mapping):
        return Grants.from_dict(cached)
    grants = grants_for_role(role)
    try:
        cache.set(key, grants.as_dict(), int(getattr(settings, "ACCOUNTS_GRANTS_CACHE_SECONDS", 300)))
    except Exception:  # noqa: BLE001
        logger.warning("grants cache write failed", exc_info=True)
    return grants


def get_grants(user) -> Grants:
    """Grants of a live, active staff user; ``EMPTY_GRANTS`` for anyone else (anonymous, service principals, inactive)."""
    if not _is_live_staff_user(user):
        return EMPTY_GRANTS
    memo = getattr(user, _MEMO_ATTR, None)
    if memo is not None:
        return memo
    role = user.role
    grants = EMPTY_GRANTS if role is None or role.deleted_at is not None else _cached_grants(user, role)
    setattr(user, _MEMO_ATTR, grants)
    return grants


def forget_grants(user) -> None:
    """Drop the per-request memo (after a role change within the same request)."""
    if hasattr(user, _MEMO_ATTR):
        delattr(user, _MEMO_ATTR)


def can(user, module: str, action: str) -> bool:
    return get_grants(user).allows(module, action)


def scope_for(user, module: str) -> str | None:
    return get_grants(user).scope(module)


# Names used by the foundation (core.permissions, core.scopes, core.dashboard) — kept as aliases.
has_permission = can
get_scope = scope_for


def invalidate_user(user_uid) -> None:
    """Orphan every cached grant set and session-liveness entry of one user."""
    bump(user_namespace(user_uid))


def invalidate_role(role_uid) -> None:
    """Orphan every cached grant set derived from one role (all of its holders)."""
    bump(role_namespace(role_uid))


# ----------------------------------------------------------------------------------------------------------------------
# Super Admin and escalation guards
# ----------------------------------------------------------------------------------------------------------------------
def is_super_admin_role(role) -> bool:
    """The seeded system role ``super-admin`` (a custom role cannot take the slug: it is reserved)."""
    return role is not None and role.deleted_at is None and role.is_system and role.slug == SUPER_ADMIN_SLUG


def is_super_admin(user) -> bool:
    return _is_live_staff_user(user) and is_super_admin_role(user.role)


def scope_covers(held: str | None, wanted: str | None) -> bool:
    """``all`` covers every scope; any narrower scope covers only itself (they are not comparable)."""
    if wanted is None:
        return True
    return held == SCOPE_ALL or held == wanted


def missing_grants(holder: Grants, permissions: Mapping | None, scopes: Mapping | None) -> dict[str, list[str]]:
    """What ``permissions``/``scopes`` would grant beyond ``holder`` — ``{module: [problems]}``; empty when covered."""
    wanted_permissions = normalise_permissions(permissions)
    wanted_scopes = normalise_scopes(scopes, wanted_permissions)
    problems: dict[str, list[str]] = {}
    for module, actions in wanted_permissions.items():
        held = holder.permissions.get(module, frozenset())
        missing = [action for action in actions if action not in held]
        if missing:
            problems.setdefault(module, []).append(f"You do not hold {', '.join(missing)} on {module}.")
        if held and not scope_covers(holder.scopes.get(module), wanted_scopes.get(module)):
            problems.setdefault(module, []).append(f"Scope {wanted_scopes.get(module)!r} is wider than yours ({holder.scopes.get(module)!r}).")
    return problems


def ensure_grants_held(actor, permissions: Mapping | None, scopes: Mapping | None, *, code: str, message: str) -> None:
    """Raise ``PermissionDenied(code)`` when ``permissions``/``scopes`` exceed what ``actor`` holds."""
    problems = missing_grants(get_grants(actor), permissions, scopes)
    if problems:
        raise PermissionDenied(code, message, errors={"permissions": [f"{module}: {text}" for module, texts in problems.items() for text in texts]})


# ----------------------------------------------------------------------------------------------------------------------
# Self-action guard
# ----------------------------------------------------------------------------------------------------------------------
_UNRESOLVED = (None, None, False)


def _target_user_ref(target, depth: int = 0) -> tuple[int | None, uuid.UUID | None, bool]:
    """``(pk, uid, recognised)`` of the user ``target`` denotes.

    ``target`` is a User, a user uid, or a record linked to a user through ``user_id``/``owner_id`` or a ``user``,
    ``owner`` or ``employee`` object (followed up to three hops: attendance day → employee → user). ``recognised`` is
    False when ``target`` has none of these links (the caller cannot tell whose record it is); a link that is present
    but empty (an employee without a Studio login) is recognised and belongs to nobody.
    """
    from accounts.models import User

    if target is None or depth > 3:
        return _UNRESOLVED
    if isinstance(target, User):
        return target.pk, target.uid, True
    if isinstance(target, uuid.UUID):
        return None, target, True
    if isinstance(target, str):
        try:
            return None, uuid.UUID(target), True
        except ValueError:
            return _UNRESOLVED
    recognised = False
    for attribute in ("user_id", "owner_id"):
        if hasattr(target, attribute):
            recognised = True
            value = getattr(target, attribute)
            if value is not None:
                return value, None, True
    # `employee_id` is an employee's key, never a user's: only the employee object is followed.
    for attribute in ("user", "owner", "employee"):
        if hasattr(target, attribute):
            recognised = True
            value = getattr(target, attribute)
            if value is not None:
                pk, uid, linked = _target_user_ref(value, depth + 1)
                if pk is not None or uid is not None or not linked:
                    return pk, uid, linked
    return None, None, recognised


def is_self(user, target) -> bool:
    """Whether ``target`` (a User, a user uid, or a record owned by / belonging to a user) is ``user``."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    pk, uid, _ = _target_user_ref(target)
    return (pk is not None and pk == user.pk) or (uid is not None and uid == getattr(user, "uid", None))


def deny_self_action(user, target, *, module: str | None = None, action: str | None = None, message: str | None = None) -> None:
    """Raise ``PermissionDenied("self_action_denied")`` when ``target`` is ``user``'s own record.

    Pass ``module``/``action`` for the registry-listed pairs (``attendance.edit``, ``leave.approve``); an unlisted
    pair is a programming error. Without them the guard always applies (e.g. deactivating yourself).
    """
    if (module is None) != (action is None):
        raise ValueError("Pass both module and action, or neither.")
    if module is not None and (module, action) not in SELF_ACTION_DENIED:
        raise ValueError(f"{module}.{action} is not a self-action-denied permission (accounts.registry.SELF_ACTION_DENIED).")
    if not _target_user_ref(target)[2]:
        # Fail closed: a record the guard cannot attribute is a programming error, never "not yours".
        raise ValueError(f"deny_self_action cannot tell whose record {type(target).__name__} is (pass a User, a user uid, or a record with user/owner/employee).")
    if is_self(user, target):
        raise PermissionDenied("self_action_denied", message or "You cannot perform this action on your own record.")
