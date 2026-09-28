"""``hr/offices/`` and ``hr/shifts/`` (module ``hr_setup``: view / edit).

Offices: CRUD + ``GET …/<uid>/summary/?day=`` (default: today in the office's zone). Shifts: CRUD. Deleting is a soft
delete refused while the row is in use (``office_in_use`` / ``shift_in_use``): deactivate instead.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from hr.filters import OfficeFilter, ShiftFilter
from hr.serializers.setup import (
    DayQuerySerializer,
    OfficeCreateSerializer,
    OfficeSerializer,
    OfficeSummarySerializer,
    OfficeUpdateSerializer,
    ShiftCreateSerializer,
    ShiftSerializer,
    ShiftUpdateSerializer,
)
from hr.services import offices, shifts

TAGS = ["hr"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, **READ_ERRORS, 409: ErrorSerializer}
DELETED = OpenApiResponse(description="Deleted.")
SETUP_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}


def crud_schema(prefix: str, read, create, update, delete_note: str):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, **READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: DELETED, **WRITE_ERRORS}, tags=TAGS, description=delete_note),
    )


class HrCrudViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)


@crud_schema("hr_offices", OfficeSerializer, OfficeCreateSerializer, OfficeUpdateSerializer, "Soft delete; 409 `office_in_use` while employees (or devices) belong to it.")
class OfficeViewSet(HrCrudViewSet):
    module = "hr_setup"
    action_permissions = {**SETUP_PERMISSIONS, "summary": "view"}
    services = {"create": offices.create_office, "update": offices.update_office, "destroy": offices.delete_office}
    serializer_class = OfficeSerializer
    write_serializers = {"create": OfficeCreateSerializer, "partial_update": OfficeUpdateSerializer}
    filterset_class = OfficeFilter
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return offices.offices_queryset()

    @extend_schema(
        operation_id="hr_offices_summary",
        parameters=[DayQuerySerializer],
        responses={200: OfficeSummarySerializer, 400: ErrorSerializer, **READ_ERRORS},
        tags=TAGS,
        description="People, leave, holiday and weekly-off for one day, plus the sections the attendance and devices packages add.",
    )
    @action(detail=True, methods=["get"])
    def summary(self, request, *args, **kwargs):
        office = self.get_object()
        query = DayQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(OfficeSummarySerializer(offices.summary(office, query.validated_data.get("day"), request.user)).data)


@crud_schema("hr_shifts", ShiftSerializer, ShiftCreateSerializer, ShiftUpdateSerializer, "Soft delete; 409 `shift_in_use` while employees or offices use it (its attendance rules go with it).")
class ShiftViewSet(HrCrudViewSet):
    module = "hr_setup"
    action_permissions = SETUP_PERMISSIONS
    services = {"create": shifts.create_shift, "update": shifts.update_shift, "destroy": shifts.delete_shift}
    serializer_class = ShiftSerializer
    write_serializers = {"create": ShiftCreateSerializer, "partial_update": ShiftUpdateSerializer}
    filterset_class = ShiftFilter
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name", "start_time", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return shifts.shifts_queryset()
