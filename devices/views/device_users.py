"""``devices/device-users/`` — terminal users, one row per ``(device, PIN)`` (module ``devices``).

view: list (``device``, ``linked``, ``search``, ``device_state``, ``software_state``, ``active_only``), detail,
``unmapped/?device=`` · edit: ``…/link/``, ``auto-link/?device=``, ``…/resolve/``, ``map-pin/``.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from devices.serializers.device_users import (
    AutoLinkQuerySerializer,
    AutoLinkResultSerializer,
    DeviceUserQuerySerializer,
    DeviceUserSerializer,
    LinkSerializer,
    MapPinResultSerializer,
    MapPinSerializer,
    ResolveSerializer,
    UnmappedQuerySerializer,
    UnmappedSerializer,
)
from devices.services import device_users, roster
from devices.views.common import READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS
from flarize.filters import normalise_filter_params

MAP_PIN_NOTE = "Punches of this PIN on this device are attributed to the employee when attendance is re-computed (queued automatically for the recent days)."


@extend_schema_view(
    list=extend_schema(
        operation_id="devices_device_users_list", parameters=[DeviceUserQuerySerializer], tags=TAGS, description="Every mapping with its current state; nothing is hidden unless asked."
    ),
    retrieve=extend_schema(operation_id="devices_device_users_retrieve", responses={200: DeviceUserSerializer, **READ_ERRORS}, tags=TAGS),
)
class DeviceUserViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {"list": "view", "retrieve": "view", "unmapped": "view", "link": "edit", "auto_link": "edit", "resolve": "edit", "map_pin": "edit"}
    http_method_names = ["get", "post"]
    lookup_value_regex = UUID_REGEX
    serializer_class = DeviceUserSerializer
    search_fields = ["pin", "name", "card", "employee__full_name", "employee__code"]
    ordering_fields = ["pin", "name", "last_seen_at", "created_at"]
    ordering = ["device__name", "pin"]

    def base_queryset(self):
        queryset = device_users.device_users_queryset()
        if self.action != "list":
            return queryset
        query = DeviceUserQuerySerializer(data=normalise_filter_params(self.request.query_params))
        query.is_valid(raise_exception=True)
        params = query.validated_data
        if params.get("device"):
            queryset = queryset.filter(device__uid=params["device"])
        if params.get("linked") is not None:
            queryset = queryset.filter(employee__isnull=not params["linked"])
        state = params.get("device_state") or (roster.ACTIVE_ON_DEVICE if params.get("active_only") else None)
        if state:
            queryset = queryset.filter(roster.device_state_q(state, queryset.values_list("device_id", flat=True).distinct()))
        if params.get("software_state"):
            queryset = queryset.filter(roster.software_state_q(params["software_state"]))
        if params.get("active_only"):
            queryset = queryset.exclude(roster.software_state_q(roster.SOFTWARE_DEACTIVATED))
        return queryset

    def _row_response(self, row):
        return Response(DeviceUserSerializer(device_users.device_users_queryset().get(pk=row.pk), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_device_users_link",
        request=LinkSerializer,
        responses={200: DeviceUserSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Link (or unlink with null) THIS device row only; other terminals are never touched.",
    )
    @action(detail=True, methods=["post"])
    def link(self, request, *args, **kwargs):
        body = LinkSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        row = device_users.link(self.get_object(), user=request.user, employee=body.validated_data["employee_uid"], expected_version=body.validated_data.get("expected_version"))
        return self._row_response(row)

    @extend_schema(
        operation_id="devices_device_users_auto_link",
        parameters=[AutoLinkQuerySerializer],
        request=None,
        responses={200: AutoLinkResultSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Link unlinked rows of active devices whose PIN equals a live employee's code; never overwrites a link.",
    )
    @action(detail=False, methods=["post"], url_path="auto-link")
    def auto_link(self, request, *args, **kwargs):
        query = AutoLinkQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(AutoLinkResultSerializer(device_users.auto_link(user=request.user, device=query.validated_data.get("device"))).data)

    @extend_schema(
        operation_id="devices_device_users_resolve",
        request=ResolveSerializer,
        responses={200: DeviceUserSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Act on one reconciliation row (confirm=true): LINK_EXISTING or CREATE_EMPLOYEE (also needs employees.create).",
    )
    @action(detail=True, methods=["post"])
    def resolve(self, request, *args, **kwargs):
        body = ResolveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        row = device_users.resolve(
            self.get_object(),
            user=request.user,
            action=data["action"],
            confirm=data["confirm"],
            employee=data.get("employee_uid"),
            employee_code=data.get("employee_code", ""),
            full_name=data.get("full_name", ""),
            office=data.get("office_uid"),
            shift=data.get("shift_uid"),
            expected_version=data.get("expected_version"),
        )
        return self._row_response(row)

    @extend_schema(
        operation_id="devices_device_users_map_pin",
        request=MapPinSerializer,
        responses={200: MapPinResultSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Attribute a PIN of one named device to an employee (creates the row when the terminal never reported it).",
    )
    @action(detail=False, methods=["post"], url_path="map-pin")
    def map_pin(self, request, *args, **kwargs):
        body = MapPinSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        row, created = device_users.map_pin(user=request.user, device=data["device_uid"], pin=data["pin"], employee=data["employee_uid"])
        row = device_users.device_users_queryset().get(pk=row.pk)
        return Response(MapPinResultSerializer({"device_user": row, "created": created, "note": MAP_PIN_NOTE}, context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_device_users_unmapped",
        parameters=[UnmappedQuerySerializer, OpenApiParameter("page", OpenApiTypes.INT), OpenApiParameter("page_size", OpenApiTypes.INT)],
        responses={200: UnmappedSerializer(many=True), 400: READ_ERRORS[401], 401: READ_ERRORS[401], 403: READ_ERRORS[403]},
        tags=TAGS,
        description="PINs of one device that produced punches but have no linked row there (from the punch store; empty until it is installed).",
    )
    @action(detail=False, methods=["get"])
    def unmapped(self, request, *args, **kwargs):
        query = UnmappedQuerySerializer(data=normalise_filter_params(request.query_params))
        query.is_valid(raise_exception=True)
        rows = device_users.unmapped(query.validated_data["device"])
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(UnmappedSerializer(page, many=True).data)
