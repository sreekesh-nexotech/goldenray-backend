"""``careers/departments/`` — CRUD (module ``departments``: view / create / edit / archive)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from careers.filters import DepartmentFilter
from careers.serializers.departments import DepartmentCreateSerializer, DepartmentSerializer, DepartmentUpdateSerializer
from careers.services import departments
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["careers"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="careers_departments_list", tags=TAGS),
    retrieve=extend_schema(operation_id="careers_departments_retrieve", responses={200: DepartmentSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="careers_departments_create", request=DepartmentCreateSerializer, responses={201: DepartmentSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="careers_departments_update", request=DepartmentUpdateSerializer, responses={200: DepartmentSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="careers_departments_delete",
        responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS},
        tags=TAGS,
        description="Refused with 409 `department_in_use` while positions belong to the department (deactivate it instead).",
    ),
)
class DepartmentViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "departments"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}
    services = {"create": departments.create_department, "update": departments.update_department, "destroy": departments.delete_department}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = DepartmentSerializer
    filterset_class = DepartmentFilter
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "sort_order", "created_at"]
    ordering = ["sort_order", "name"]

    def base_queryset(self):
        return departments.departments_queryset()

    def get_serializer_class(self):
        return {"create": DepartmentCreateSerializer, "partial_update": DepartmentUpdateSerializer}.get(self.action, DepartmentSerializer)
