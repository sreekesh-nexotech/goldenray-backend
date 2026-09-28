"""Base classes for every API view. Views stay thin: HTTP shape here, business logic in ``<app>/services``.

Staff (``/api/<version>/``):
    ``BaseViewSet`` / ``BaseAPIView`` — JWT auth, ``HasModulePermission`` (default deny), throttle scope ``staff``,
    ``lookup_field = "uid"``. Record scope is applied by :meth:`BaseViewSet.get_queryset`; subclasses override
    :meth:`base_queryset` only (``core.E003`` rejects a ``get_queryset`` override). Writes are delegated to the
    functions in ``services`` (``{"create": fn, "update": fn, "destroy": fn}``) by ``core.views.mixins`` — use those
    mixins, not ``rest_framework.mixins`` (``core.E005``); there is no ORM write in a view and no hard delete.

Public (``/api/public/<version>/``):
    ``PublicAPIView`` / ``PublicGenericViewSet`` — ``AllowAny``, ``authentication_classes = []``, throttle
    ``public_read`` for safe methods and ``public_write`` otherwise.

Agent (``/api/agent/<version>/``): ``AgentAPIView`` — service-token auth, throttle ``agent``.
Customer (``/api/customer/<version>/``): ``CustomerAPIView`` — no session auth; each view verifies its signed
token (+OTP) through its service; throttle ``customer``.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from rest_framework import permissions, viewsets
from rest_framework.views import APIView

from core import scopes
from core.permissions import HasModulePermission, IsServicePrincipal
from core.service_credentials import ServiceTokenAuthentication
from core.views.mixins import ServiceWritesMixin

SAFE_METHODS = permissions.SAFE_METHODS


class StaffViewMixin:
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    throttle_scope = "staff"
    module: str | None = None  # registry module checked by HasModulePermission
    action_permissions: dict = {}  # {drf_action | HTTP method: registry action | (module, action)}
    record_scope_module: str | None = None  # registry module whose record scope filters the queryset (default: module)

    def scope_queryset(self, queryset, module: str | None = None):
        """Apply record scope for ``module`` (default: the view's module)."""
        return scopes.apply(queryset, self.request.user, module or self.scope_module)

    @property
    def scope_module(self) -> str:
        module = self.record_scope_module or self.module
        if not module:
            raise ImproperlyConfigured(f"{self.__class__.__name__} must declare `module`.")
        return module

    def expected_version(self, data=None):
        return ServiceWritesMixin.expected_version(self, data)


class BaseViewSet(ServiceWritesMixin, StaffViewMixin, viewsets.GenericViewSet):
    """Staff viewset. Combine with the ``core.views.mixins`` mixins and declare ``action_permissions`` + ``services``."""

    lookup_field = "uid"
    lookup_url_kwarg = "uid"

    def base_queryset(self):
        raise ImproperlyConfigured(f"{self.__class__.__name__} must implement base_queryset().")

    def get_queryset(self):
        return self.scope_queryset(self.base_queryset())


class BaseAPIView(StaffViewMixin, APIView):
    """Staff APIView; ``action_permissions`` is keyed by HTTP method (``{"GET": "view", "POST": "edit"}``)."""


class PublicViewMixin:
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    throttle_scope: str | None = None  # set explicitly to override the read/write default
    read_throttle_scope = "public_read"
    write_throttle_scope = "public_write"

    def get_throttle_scope(self, request) -> str:
        if self.throttle_scope:
            return self.throttle_scope
        return self.read_throttle_scope if request.method in SAFE_METHODS else self.write_throttle_scope


class PublicAPIView(PublicViewMixin, APIView):
    """Website endpoint: anonymous, throttled per client IP, cached through ``flarize.cache_utils``."""


class PublicGenericViewSet(PublicViewMixin, viewsets.GenericViewSet):
    lookup_field = "uid"
    lookup_url_kwarg = "uid"


class AgentAPIView(APIView):
    """Office-agent endpoint: ``Authorization: Bearer fl_…`` service token; throttled per token."""

    authentication_classes = [ServiceTokenAuthentication]
    permission_classes = [IsServicePrincipal]
    service_kinds = ("AGENT",)
    throttle_scope = "agent"


class CustomerAPIView(APIView):
    """Customer self-service link: the view's service verifies the signed token (+OTP); throttled per client."""

    authentication_classes: list = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "customer"
