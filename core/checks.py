"""System checks that enforce the API contract at startup (and in CI via ``manage.py check``).

* ``core.E001`` a view using ``HasModulePermission`` declares no ``module``/``action_permissions``;
* ``core.E002`` ``action_permissions`` maps to a module/action the closed registry does not contain;
* ``core.E003`` a ``BaseViewSet`` subclass overrides ``get_queryset()`` (record scope must stay in the base class);
* ``core.E004`` a DRF view is routed outside the versioned surfaces;
* ``core.E005`` a routed generic view still uses DRF's default ``perform_create/update/destroy`` (an ORM write or a
  hard delete in the view) instead of the service-delegating ``core.views.mixins``.
"""

from __future__ import annotations

from django.core import checks

VERSIONED_PREFIX_RE = r"^(api/(public/|agent/|customer/)?\(\?P<version>[^)]+\)/|api/schema/\(\?P<version>[^)]+\)/|api/docs/|legacy/|iclock/)"


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


def check_view_permissions(app_configs=None, **kwargs):
    import re

    from rest_framework.generics import GenericAPIView

    from accounts.registry import is_allowed
    from core.permissions import HasModulePermission
    from core.views.base import BaseViewSet
    from core.views.mixins import DRF_DEFAULT_WRITES

    errors = []
    prefix_re = re.compile(VERSIONED_PREFIX_RE)
    for route, cls, already_seen in _iter_drf_views():
        if not prefix_re.match(route):
            errors.append(checks.Error(f"DRF view {cls.__module__}.{cls.__qualname__} is routed outside the versioned surfaces: {route!r}", id="core.E004"))
        if already_seen:
            continue
        uses_module_permission = any(isinstance(perm, type) and issubclass(perm, HasModulePermission) for perm in _permission_classes(cls))
        if uses_module_permission:
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
    return errors


checks.register(check_view_permissions, checks.Tags.urls)
