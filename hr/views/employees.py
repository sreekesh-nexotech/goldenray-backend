"""``hr/employees/`` (module ``employees``: view / create / edit / archive; record scope all / office / self).

view: list (active only unless ``is_active``/``include_inactive``), detail, ``device-mappings/``, ``dependencies/`` ·
create · edit: fields, ``link-user/``, ``unlink-user/``, ``photo/``, ``reconcile-devices/`` · archive:
``deactivate/``, ``activate/``, ``DELETE`` (only without history).
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from flarize.filters import normalise_filter_params
from hr.filters import EmployeeFilter
from hr.serializers.employees import (
    EmployeeActionSerializer,
    EmployeeCreateSerializer,
    EmployeeDeactivateSerializer,
    EmployeeSerializer,
    EmployeeUpdateSerializer,
    LinkResultSerializer,
    LinkUserSerializer,
)
from hr.services import employee_links, employees
from hr.views.employee_extras import EmployeeExtrasMixin
from hr.views.setup import DELETED, READ_ERRORS, UUID_REGEX, WRITE_ERRORS

TAGS = ["hr"]
TRUE = ("1", "true", "yes")


@extend_schema_view(
    list=extend_schema(
        operation_id="hr_employees_list",
        parameters=[OpenApiParameter("include_inactive", OpenApiTypes.BOOL, description="List deactivated employees too (default: active only).")],
        tags=TAGS,
    ),
    retrieve=extend_schema(operation_id="hr_employees_retrieve", responses={200: EmployeeSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="hr_employees_create", request=EmployeeCreateSerializer, responses={201: EmployeeSerializer, **WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="hr_employees_update", request=EmployeeUpdateSerializer, responses={200: EmployeeSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="hr_employees_delete",
        responses={204: DELETED, **WRITE_ERRORS},
        tags=TAGS,
        description="Only an employee without history (409 `employee_has_history`: deactivate instead). A linked login is retired.",
    ),
)
class EmployeeViewSet(EmployeeExtrasMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "employees"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "device_mappings": "view",
        "dependencies": "view",
        "create": "create",
        "partial_update": "edit",
        "link_user": "edit",
        "unlink_user": "edit",
        "photo": "edit",
        "remove_photo": "edit",
        "reconcile_devices": "edit",
        "deactivate": "archive",
        "activate": "archive",
        "destroy": "archive",
    }
    services = {"create": employees.create_employee, "update": employees.update_employee, "destroy": employees.delete_employee}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = EmployeeSerializer
    filterset_class = EmployeeFilter
    search_fields = ["code", "full_name", "email", "department", "designation"]
    ordering_fields = ["code", "full_name", "joined_on", "created_at"]
    ordering = ["full_name"]

    def base_queryset(self):
        queryset = employees.employees_queryset()
        if self.action == "list":
            params = normalise_filter_params(self.request.query_params)
            if "is_active" not in params and params.get("include_inactive", "").lower() not in TRUE:
                queryset = queryset.filter(is_active=True)
        return queryset

    def get_serializer_class(self):
        return {"create": EmployeeCreateSerializer, "partial_update": EmployeeUpdateSerializer}.get(self.action, EmployeeSerializer)

    def _lifecycle(self, request, service, serializer_class=EmployeeActionSerializer):
        employee = self.get_object()
        body = serializer_class(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        updated = service(employee, user=request.user, expected_version=data.pop("expected_version", None), **data)
        return self._employee_response(updated)

    @extend_schema(
        operation_id="hr_employees_deactivate",
        request=EmployeeDeactivateSerializer,
        responses={200: EmployeeSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Emits `hr.employee_deactivated`: the linked login is deactivated and signed out. 403 `self_action_denied` on your own record.",
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, *args, **kwargs):
        return self._lifecycle(request, employees.deactivate_employee, EmployeeDeactivateSerializer)

    @extend_schema(
        operation_id="hr_employees_activate",
        request=EmployeeActionSerializer,
        responses={200: EmployeeSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Back to active (`left_on` cleared). The login is not restored: re-link it or reactivate it in Users.",
    )
    @action(detail=True, methods=["post"])
    def activate(self, request, *args, **kwargs):
        return self._lifecycle(request, employees.activate_employee)

    def _link_response(self, employee, outcome):
        employee = employees.employees_queryset().get(pk=employee.pk)
        return Response(LinkResultSerializer({"employee": employee, **outcome}, context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="hr_employees_link_user",
        request=LinkUserSerializer,
        responses={200: LinkResultSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description=(
            "Link an existing login (`user_uid`) or create a Staff login with an invitation (`email`). The Staff role replaces the "
            "login's role only when that role grants nothing beyond Staff. 409 `employee_already_linked`, `user_already_linked`, "
            "`user_inactive`, `employee_inactive`; 403 when the login holds grants you do not."
        ),
    )
    @action(detail=True, methods=["post"], url_path="link-user")
    def link_user(self, request, *args, **kwargs):
        employee = self.get_object()
        body = LinkUserSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        linked, outcome = employee_links.link_user(employee, user=request.user, data=data, expected_version=data.pop("expected_version", None))
        return self._link_response(linked, outcome)

    @extend_schema(
        operation_id="hr_employees_unlink_user",
        request=EmployeeActionSerializer,
        responses={200: LinkResultSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Remove the login link; a Staff-only login is deactivated (it exists only for the link). 409 `employee_not_linked`.",
    )
    @action(detail=True, methods=["post"], url_path="unlink-user")
    def unlink_user(self, request, *args, **kwargs):
        employee = self.get_object()
        body = EmployeeActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        unlinked, outcome = employee_links.unlink_user(employee, user=request.user, expected_version=body.validated_data.get("expected_version"), note=body.validated_data["note"])
        return self._link_response(unlinked, outcome)
