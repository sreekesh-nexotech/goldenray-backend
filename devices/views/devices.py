"""``devices/`` — terminals (module ``devices``).

view: list, detail, ``mapping/``, ``…/employee-reconciliation/``, ``…/user-reconciliation/``, ``…/logs/`` · create ·
edit: ``PATCH`` · sync: ``…/refresh-employees/`` · manage: ``DELETE``, ``…/rehome/``, ``…/adms/enable/``, ``…/adms/disable/``.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from devices.filters import DeviceFilter, SyncLogFilter
from devices.models import SyncLog
from devices.serializers.devices import DeviceCreateSerializer, DeviceSerializer, DeviceUpdateSerializer, MappingReportSerializer
from devices.serializers.refs import SyncLogSerializer
from devices.services import devices
from devices.views.common import DELETED, LOG_PARAMETERS, READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, MomentMixin, SyncLogCursorPagination, paginate_logs
from devices.views.device_actions import DeviceActionsMixin


@extend_schema_view(
    list=extend_schema(operation_id="devices_list", tags=TAGS, description="Terminals with their derived health (no stored online flag)."),
    retrieve=extend_schema(operation_id="devices_retrieve", responses={200: DeviceSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(
        operation_id="devices_create",
        request=DeviceCreateSerializer,
        responses={201: DeviceSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="400 `device_not_locatable` without an address or a serial; 409 `device_serial_taken`.",
    ),
    partial_update=extend_schema(
        operation_id="devices_update", request=DeviceUpdateSerializer, responses={200: DeviceSerializer, **WRITE_ERRORS}, tags=TAGS, description="The agent changes only through rehome/."
    ),
    destroy=extend_schema(
        operation_id="devices_delete", responses={204: DELETED, **WRITE_ERRORS}, tags=TAGS, description="Soft delete of a device without history (409 `device_has_history`: deactivate it)."
    ),
)
class DeviceViewSet(DeviceActionsMixin, MomentMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "mapping": "view",
        "logs": "view",
        "employee_reconciliation": "view",
        "user_reconciliation": "view",
        "create": "create",
        "partial_update": "edit",
        "refresh_employees": "sync",
        "destroy": "manage",
        "rehome": "manage",
        "adms_enable": "manage",
        "adms_disable": "manage",
    }
    services = {"create": devices.create_device, "update": devices.update_device, "destroy": devices.delete_device}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = DeviceSerializer
    write_serializers = {"create": DeviceCreateSerializer, "partial_update": DeviceUpdateSerializer}
    filterset_class = DeviceFilter
    search_fields = ["name", "serial_number", "expected_serial", "ip_address", "model"]
    ordering_fields = ["name", "serial_number", "created_at", "last_seen_at", "adms_last_seen_at"]
    ordering = ["name"]

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)

    def base_queryset(self):
        return devices.devices_queryset()

    @extend_schema(
        operation_id="devices_mapping",
        responses={200: MappingReportSerializer, 401: READ_ERRORS[401], 403: READ_ERRORS[403]},
        tags=TAGS,
        description="The device → office → agent chain and where it contradicts itself (read-only).",
    )
    @action(detail=False, methods=["get"])
    def mapping(self, request, *args, **kwargs):
        return Response(MappingReportSerializer(devices.mapping_report(), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_logs", parameters=LOG_PARAMETERS, responses={200: SyncLogSerializer(many=True), **READ_ERRORS}, tags=TAGS, description="Sync logs of the device, newest first (cursor)."
    )
    @action(detail=True, methods=["get"], pagination_class=SyncLogCursorPagination)
    def logs(self, request, *args, **kwargs):
        device = self.get_object()
        logs = SyncLogFilter(request.query_params, queryset=SyncLog.objects.filter(device=device).select_related("device__office", "agent")).qs
        return paginate_logs(self, logs, SyncLogSerializer)
