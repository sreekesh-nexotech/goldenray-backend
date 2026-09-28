"""Record-level scope filters — the one central record-visibility resolver (standard §3.2 step 4).

Each module that supports a narrower scope registers one filter per scope::

    @scopes.register("customers", "owned")
    def owned_customers(queryset, user):
        return queryset.filter(owner=user)

:func:`apply` resolves the user's scope for the module (``accounts.services.authz``) and narrows the queryset.
It **fails closed**: no user, no scope for the module, a scope the registry does not allow for the module, or no
filter registered for that scope → ``queryset.none()``. ``all`` needs no filter (identity) but must still be an
allowed scope for the module. Scopes never widen: ``owned`` never includes a teammate's rows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.db.models import QuerySet

logger = logging.getLogger("flarize.scopes")

ALL = "all"
ScopeFilter = Callable[[QuerySet, object], QuerySet]
_FILTERS: dict[tuple[str, str], ScopeFilter] = {}


def register(module: str, scope: str):
    from accounts.registry import MODULES, SCOPES

    if module not in MODULES:
        raise ValueError(f"Unknown registry module {module!r}.")
    if scope not in SCOPES or scope not in MODULES[module].scopes:
        raise ValueError(f"Scope {scope!r} is not allowed for module {module!r}.")
    if scope == ALL:
        raise ValueError("The 'all' scope is the identity filter and is not registered.")

    def decorator(fn: ScopeFilter) -> ScopeFilter:
        _FILTERS[(module, scope)] = fn
        return fn

    return decorator


def registered_filter(module: str, scope: str) -> ScopeFilter | None:
    return _FILTERS.get((module, scope))


def resolve_scope(user, module: str) -> str | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from accounts.services.authz import scope_for

    return scope_for(user, module)


def apply(queryset: QuerySet, user, module: str) -> QuerySet:
    """Narrow ``queryset`` to what ``user`` may see in ``module`` (``.none()`` when nothing applies)."""
    from accounts.registry import MODULES

    spec = MODULES.get(module)
    scope = resolve_scope(user, module)
    if spec is None or scope is None or scope not in spec.scopes:
        return queryset.none()
    if scope == ALL:
        return queryset
    fn = _FILTERS.get((module, scope))
    if fn is None:
        logger.error("no scope filter registered", extra={"registry_module": module, "scope": scope})
        return queryset.none()
    return fn(queryset, user)
