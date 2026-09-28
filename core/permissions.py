"""Module-level RBAC for staff views (standard §3.2 step 2). Default deny; no superuser/staff bypass.

A view declares::

    module = "catalog"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "activate": "approve"}

* ViewSets are keyed by DRF action (``list``, ``retrieve``, ``create``, ``partial_update``, custom ``@action``s).
* APIViews are keyed by HTTP method (``GET``, ``POST`` …); ``HEAD`` falls back to ``GET``.
* A value may be ``"action"`` (on ``view.module``) or ``("other_module", "action")``.
* Anything unmapped — including ``OPTIONS`` unless mapped — is denied. Unknown modules/actions are denied (and
  flagged by the ``core.E00x`` system checks at startup).

Grants come from ``accounts.services.authz.get_grants(user)`` (resolved server-side, never from the token).
"""

from __future__ import annotations

import logging

from rest_framework.permissions import BasePermission

logger = logging.getLogger("flarize.permissions")


def required_permission(request, view) -> tuple[str, str] | None:
    """The ``(module, action)`` a request needs, or ``None`` when the view does not map it."""
    mapping = getattr(view, "action_permissions", None) or {}
    if hasattr(view, "get_extra_actions"):  # a ViewSet: keyed by DRF action; no action (unrouted method) → deny
        drf_action = getattr(view, "action", None)
        value = mapping.get(drf_action) if drf_action else None
    else:
        method = request.method.upper()
        value = mapping.get(method)
        if value is None and method == "HEAD":
            value = mapping.get("GET")
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return str(value[0]), str(value[1])
    module = getattr(view, "module", None)
    if not module:
        return None
    return module, str(value)


class HasModulePermission(BasePermission):
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        needed = required_permission(request, view)
        if needed is None:
            return False
        module, action = needed
        from accounts.registry import is_allowed
        from accounts.services.authz import has_permission

        if not is_allowed(module, action):
            logger.error("view maps to an unknown registry permission", extra={"view": view.__class__.__name__, "registry_module": module, "registry_action": action})
            return False
        return has_permission(user, module, action)


class IsServicePrincipal(BasePermission):
    """Machine surfaces (``/api/agent/…``): only a service-token principal of ``view.service_kinds`` passes."""

    message = "A valid service token is required."

    def has_permission(self, request, view) -> bool:
        from core.service_credentials import ServicePrincipal

        user = request.user
        kinds = getattr(view, "service_kinds", ("AGENT",))
        return isinstance(user, ServicePrincipal) and user.kind in kinds
