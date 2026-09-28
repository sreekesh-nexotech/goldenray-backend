"""``roles/`` — role CRUD and the permission registry (module ``roles``).

``roles.view`` list/detail/registry · ``roles.create`` create · ``roles.edit`` update · ``roles.manage`` delete.
Registry normalisation and escalation guards live in ``accounts.services.roles``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts import registry
from accounts.filters import RoleFilter
from accounts.serializers.roles import RegistrySerializer, RoleCreateSerializer, RoleSerializer, RoleUpdateSerializer
from accounts.services import roles
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["roles"]
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="roles_list", tags=TAGS),
    retrieve=extend_schema(operation_id="roles_retrieve", responses={200: RoleSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="roles_create", request=RoleCreateSerializer, responses={201: RoleSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="roles_update", request=RoleUpdateSerializer, responses={200: RoleSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="roles_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
)
class RoleViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "roles"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "registry": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "manage",
    }
    services = {"create": roles.create_role, "update": roles.update_role, "destroy": roles.delete_role}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = RoleSerializer
    filterset_class = RoleFilter
    search_fields = ["name", "slug", "description"]
    ordering_fields = ["name", "slug", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return roles.roles_queryset()

    def get_serializer_class(self):
        return {"create": RoleCreateSerializer, "partial_update": RoleUpdateSerializer}.get(self.action, RoleSerializer)

    @extend_schema(operation_id="roles_registry", responses={200: RegistrySerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=TAGS)
    @action(detail=False, methods=["get"], pagination_class=None, filter_backends=[])
    def registry(self, request, *args, **kwargs):
        return Response(registry.as_dict())
