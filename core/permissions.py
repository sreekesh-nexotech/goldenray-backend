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
        from accounts.services.authz import can

        if not is_allowed(module, action):
            logger.error("view maps to an unknown registry permission", extra={"view": view.__class__.__name__, "registry_module": module, "registry_action": action})
            return False
        return can(user, module, action)


class IsServicePrincipal(BasePermission):
    """Machine surfaces (``/api/agent/…``): only a service-token principal of ``view.service_kinds`` passes."""

    message = "A valid service token is required."

    def has_permission(self, request, view) -> bool:
        from core.service_credentials import ServicePrincipal

        user = request.user
        kinds = getattr(view, "service_kinds", ("AGENT",))
        return isinstance(user, ServicePrincipal) and user.kind in kinds


class ApiDocsAccess(BasePermission):
    """``/api/docs/`` and ``/api/schema/<version>/`` (PLAN §5.4 "staff only, allow-list IPs").

    Open when ``API_DOCS_PUBLIC`` (dev, staging); otherwise only for client addresses inside
    ``API_DOCS_ALLOWED_NETWORKS`` — the same office/VPN networks as nginx's ``snippets/docs-allow.conf``, checked
    again here so a proxy misconfiguration cannot expose the schema. No JWT: the Swagger UI is a browser
    navigation, which cannot carry one. The client address comes from :func:`flarize.client_ip.get_client_ip`
    (``X-Forwarded-For`` only via trusted proxies). An empty list denies everyone (fail-closed).
    """

    message = "The API documentation is only available from the allow-listed office networks."

    def has_permission(self, request, view) -> bool:
        from django.conf import settings

        from flarize.client_ip import get_client_ip, is_trusted, parse_ip, parse_networks

        if getattr(settings, "API_DOCS_PUBLIC", False):
            return True
        networks = parse_networks(tuple(getattr(settings, "API_DOCS_ALLOWED_NETWORKS", ()) or ()))
        return bool(networks) and is_trusted(parse_ip(get_client_ip(request)), networks)
