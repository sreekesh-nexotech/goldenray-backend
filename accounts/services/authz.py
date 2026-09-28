"""Authorization data for a user: the grants of their role, normalised against the registry.

Resolved server-side on every request (never from the token), memoised on the user object for the duration of the
request. The accounts work package adds a version-keyed cache (``accounts:role:<uid>`` namespaces).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from accounts.registry import normalise_permissions, normalise_scopes

_MEMO_ATTR = "_flarize_grants"


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


EMPTY_GRANTS = Grants()


def grants_for_role(role) -> Grants:
    if role is None or role.deleted_at is not None:
        return EMPTY_GRANTS
    permissions = normalise_permissions(role.permissions)
    scopes = normalise_scopes(role.scopes, permissions)
    return Grants({module: frozenset(actions) for module, actions in permissions.items()}, scopes)


def get_grants(user) -> Grants:
    """Grants of a live, active staff user; ``EMPTY_GRANTS`` for anyone else (anonymous, service principals, inactive)."""
    from accounts.models import User

    if not isinstance(user, User) or not user.is_authenticated or not user.is_active or user.deleted_at is not None:
        return EMPTY_GRANTS
    memo = getattr(user, _MEMO_ATTR, None)
    if memo is not None:
        return memo
    grants = grants_for_role(user.role)
    setattr(user, _MEMO_ATTR, grants)
    return grants


def forget_grants(user) -> None:
    """Drop the per-request memo (after a role change within the same request)."""
    if hasattr(user, _MEMO_ATTR):
        delattr(user, _MEMO_ATTR)


def has_permission(user, module: str, action: str) -> bool:
    return get_grants(user).allows(module, action)


def get_scope(user, module: str) -> str | None:
    return get_grants(user).scope(module)
