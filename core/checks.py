"""System checks that enforce the API contract at startup (and in CI via ``manage.py check``).

* ``core.E001`` a view using ``HasModulePermission`` declares no ``module``/``action_permissions``;
* ``core.E002`` ``action_permissions`` maps to a module/action the closed registry does not contain;
* ``core.E003`` a ``BaseViewSet`` subclass overrides ``get_queryset()`` (record scope must stay in the base class);
* ``core.E004`` a DRF view is routed outside the versioned surfaces;
* ``core.E005`` a routed generic view still uses DRF's default ``perform_create/update/destroy`` (an ORM write or a
  hard delete in the view) instead of the service-delegating ``core.views.mixins``;
* ``core.E006`` a view on the staff surface (``/api/<version>/``) does not use ``HasModulePermission`` (a plain
  ``APIView`` with ``IsAuthenticated``, or a non-DRF view) and is not in :data:`STAFF_VIEWS_WITHOUT_MODULE_PERMISSION`.
  Default deny is thereby structural: forgetting ``module``/``action_permissions`` fails ``manage.py check``.
"""

from __future__ import annotations

from django.core import checks

VERSIONED_PREFIX_RE = r"^(api/(public/|agent/|customer/)?\(\?P<version>[^)]+\)/|api/schema/\(\?P<version>[^)]+\)/|api/docs/|legacy/|iclock/)"
STAFF_PREFIX_RE = r"^api/\(\?P<version>[^)]+\)/"

# Staff-surface views that authorize without HasModulePermission, each on purpose. Adding an entry is a shared-code
# change (list it in the WP report) and needs a DEVIATIONS.md entry unless PLAN already prescribes the behaviour.
_AUTH_SELF_SERVICE = "auth self-service (PLAN §3.1): acts only on the caller's own account or credentials"
STAFF_VIEWS_WITHOUT_MODULE_PERMISSION: dict[str, str] = {
    "accounts.views.auth.LoginView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.RefreshView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.LogoutView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.MeView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.PasswordChangeView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.PasswordResetRequestView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.PasswordResetView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.SessionListView": _AUTH_SELF_SERVICE,
    "accounts.views.auth.SessionDetailView": _AUTH_SELF_SERVICE,
    "media.views.download.MediaDownloadView": "DV-13: the short-lived signed token is the capability (browser navigation, no JWT)",
    "documents.views.jobs.DocumentDownloadView": "DV-13: the single-use signed token is the capability (browser navigation, no JWT)",
    "documents.views.jobs.RenderJobDetailView": "DV-14: the permission depends on the job's kind; documents.access.ensure_can_view (403, then 404)",
    "documents.views.jobs.RenderJobDownloadUrlView": "DV-14: the permission depends on the job's kind; documents.access.ensure_can_view (403, then 404)",
}


def _permission_classes(view_cls) -> list:
    return list(getattr(view_cls, "permission_classes", []) or [])


def _iter_drf_views():
    from rest_framework.views import APIView

    from flarize.versioning import view_class, walk_patterns

    seen = set()
    for route, pattern in walk_patterns():
        cls = view_class(pattern)
        if cls is None or not isinstance(cls, type) or not issubclass(cls, APIView):
            continue
        yield route, cls, (cls in seen)
        seen.add(cls)


def _dotted(obj) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _uses_module_permission(cls) -> bool:
    from core.permissions import HasModulePermission

    return any(isinstance(perm, type) and issubclass(perm, HasModulePermission) for perm in _permission_classes(cls))


def staff_views_without_module_permission() -> dict[str, str]:
    """``{dotted view: route}`` for every view routed on the staff surface that does not use HasModulePermission."""
    import re

    from rest_framework.views import APIView

    from flarize.versioning import view_class, walk_patterns

    staff_re = re.compile(STAFF_PREFIX_RE)
    found: dict[str, str] = {}
    for route, pattern in walk_patterns():
        if not staff_re.match(route):
            continue
        cls = view_class(pattern)
        if isinstance(cls, type) and issubclass(cls, APIView):
            if not _uses_module_permission(cls):
                found.setdefault(_dotted(cls), route)
        else:  # a plain Django view: no authentication or RBAC at all
            found.setdefault(_dotted(cls if isinstance(cls, type) else pattern.callback), route)
    return found


def check_view_permissions(app_configs=None, **kwargs):
    import re

    from rest_framework.generics import GenericAPIView

    from accounts.registry import is_allowed
    from core.views.base import BaseViewSet
    from core.views.mixins import DRF_DEFAULT_WRITES

    errors = []
    prefix_re = re.compile(VERSIONED_PREFIX_RE)
    for route, cls, already_seen in _iter_drf_views():
        if not prefix_re.match(route):
            errors.append(checks.Error(f"DRF view {cls.__module__}.{cls.__qualname__} is routed outside the versioned surfaces: {route!r}", id="core.E004"))
        if already_seen:
            continue
        if _uses_module_permission(cls):
            mapping = getattr(cls, "action_permissions", None) or {}
            if not mapping or (not getattr(cls, "module", None) and not all(isinstance(value, (tuple, list)) for value in mapping.values())):
                errors.append(checks.Error(f"{cls.__module__}.{cls.__qualname__} uses HasModulePermission but declares no module/action_permissions.", id="core.E001"))
            for key, value in mapping.items():
                module, action = (value[0], value[1]) if isinstance(value, (tuple, list)) else (getattr(cls, "module", None), value)
                if not module or not is_allowed(module, action):
                    errors.append(checks.Error(f"{cls.__module__}.{cls.__qualname__}.action_permissions[{key!r}] = {value!r} is not in the permission registry.", id="core.E002"))
        if issubclass(cls, BaseViewSet) and cls.get_queryset is not BaseViewSet.get_queryset:
            errors.append(checks.Error(f"{cls.__module__}.{cls.__qualname__} overrides get_queryset(); override base_queryset() so record scope is always applied.", id="core.E003"))
        if issubclass(cls, GenericAPIView):
            for name, default in DRF_DEFAULT_WRITES.items():
                if getattr(cls, name, None) is default:
                    errors.append(checks.Error(f"{cls.__module__}.{cls.__qualname__}.{name} is DRF's default (ORM write/hard delete in the view); use core.views.mixins.", id="core.E005"))
    for name, route in sorted(staff_views_without_module_permission().items()):
        if name not in STAFF_VIEWS_WITHOUT_MODULE_PERMISSION:
            errors.append(
                checks.Error(
                    f"{name} is routed on the staff surface ({route!r}) without HasModulePermission; extend core.views.BaseAPIView/BaseViewSet "
                    "with module + action_permissions (default deny), or add a documented exemption to core.checks.STAFF_VIEWS_WITHOUT_MODULE_PERMISSION.",
                    id="core.E006",
                )
            )
    return errors


checks.register(check_view_permissions, checks.Tags.urls)
