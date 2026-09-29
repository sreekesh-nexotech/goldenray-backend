"""``devices/protocol-mappings/`` (CRUD + ``observed/``) and the ADMS administration views (module ``devices``).

Protocol mappings: view list/detail/``observed/`` · create · edit ``PATCH`` · manage ``DELETE``.
ADMS: ``GET devices/adms/status/``, ``GET devices/adms/requests/?serial=&kind=&device=`` (cursor, newest first),
``GET devices/adms/unknown-devices/`` — all ``view``.
"""

from __future__ import annotations

from datetime import timedelta

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.flags import flag_enabled
from core.views import BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from devices.filters import AdmsRequestFilter, AdmsUnknownDeviceFilter, ProtocolMappingFilter
from devices.models import AdmsRequest, AdmsUnknownDevice
from devices.serializers.protocol import (
    AdmsRequestSerializer,
    AdmsStatusSerializer,
    AdmsUnknownDeviceSerializer,
    ObservedSerializer,
    ProtocolMappingCreateSerializer,
    ProtocolMappingSerializer,
    ProtocolMappingUpdateSerializer,
)
from devices.services import adms_evidence, protocol
from devices.services.common import now, offline_seconds, online_seconds, setting
from devices.services.devices import devices_queryset
from devices.views.common import DELETED, READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, AdmsRequestCursorPagination

ADMS_STATUS_DEVICE_LIMIT = 500


@extend_schema_view(
    list=extend_schema(operation_id="devices_protocol_mappings_list", tags=TAGS),
    retrieve=extend_schema(operation_id="devices_protocol_mappings_retrieve", responses={200: ProtocolMappingSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(
        operation_id="devices_protocol_mappings_create",
        request=ProtocolMappingCreateSerializer,
        responses={201: ProtocolMappingSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="409 `protocol_mapping_exists`.",
    ),
    partial_update=extend_schema(operation_id="devices_protocol_mappings_update", request=ProtocolMappingUpdateSerializer, responses={200: ProtocolMappingSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="devices_protocol_mappings_delete", responses={204: DELETED, **WRITE_ERRORS}, tags=TAGS),
)
class ProtocolMappingViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {"list": "view", "retrieve": "view", "observed": "view", "create": "create", "partial_update": "edit", "destroy": "manage"}
    services = {"create": protocol.create_mapping, "update": protocol.update_mapping, "destroy": protocol.delete_mapping}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = ProtocolMappingSerializer
    filterset_class = ProtocolMappingFilter
    search_fields = ["meaning_code", "label", "device_platform", "firmware_version"]
    ordering_fields = ["field", "raw_value", "created_at"]
    ordering = ["field", "raw_value"]

    def get_serializer_class(self):
        return {"create": ProtocolMappingCreateSerializer, "partial_update": ProtocolMappingUpdateSerializer}.get(self.action, ProtocolMappingSerializer)

    def base_queryset(self):
        return protocol.mappings_queryset()

    @extend_schema(
        operation_id="devices_protocol_mappings_observed",
        responses={200: ObservedSerializer, 401: READ_ERRORS[401], 403: READ_ERRORS[403]},
        tags=TAGS,
        description="Raw status/punch codes the punch store has seen, with counts and their mapping.",
    )
    @action(detail=False, methods=["get"])
    def observed(self, request, *args, **kwargs):
        return Response(ObservedSerializer(protocol.observed()).data)


class AdmsStatusView(BaseAPIView):
    module = "devices"
    action_permissions = {"GET": "view"}

    def get_serializer_context(self):
        return {"request": self.request, "view": self, "at": now()}

    @extend_schema(
        operation_id="devices_adms_status",
        responses={200: AdmsStatusSerializer, 401: READ_ERRORS[401], 403: READ_ERRORS[403]},
        tags=TAGS,
        description="What the receiver is and what it has heard from.",
    )
    def get(self, request, *args, **kwargs):
        pushing = list(devices_queryset().filter(adms_enabled=True)[: ADMS_STATUS_DEVICE_LIMIT + 1])
        payload = {
            "enabled": flag_enabled("ADMS_RECEIVER"),
            "device_path": "/iclock/<device token>/cdata",
            "public_base_url": setting("DEVICES_ADMS_PUBLIC_BASE_URL", ""),
            "online_seconds": online_seconds(),
            "offline_seconds": offline_seconds(),
            "retention_days": adms_evidence.retention_days(),
            "requests_last_24h": AdmsRequest.objects.filter(received_at__gte=now() - timedelta(days=1)).count(),
            "unknown_devices": AdmsUnknownDevice.objects.count(),
            "missing_partitions": adms_evidence.missing_upcoming(2),
            "devices": pushing[:ADMS_STATUS_DEVICE_LIMIT],
            "devices_truncated": len(pushing) > ADMS_STATUS_DEVICE_LIMIT,
        }
        return Response(AdmsStatusSerializer(payload, context=self.get_serializer_context()).data)


@extend_schema_view(list=extend_schema(operation_id="devices_adms_requests_list", tags=TAGS, description="The raw protocol log, newest first (decoded text excerpt; the bytes stay in the database)."))
class AdmsRequestViewSet(ListModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = AdmsRequestSerializer
    filterset_class = AdmsRequestFilter
    pagination_class = AdmsRequestCursorPagination

    def base_queryset(self):
        return AdmsRequest.objects.select_related("device__office").defer("body")


@extend_schema_view(
    list=extend_schema(
        operation_id="devices_adms_unknown_devices_list", tags=TAGS, description="Quarantined serials (nothing they sent was ingested; nothing is ever attached to an office automatically)."
    )
)
class AdmsUnknownDeviceViewSet(ListModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = AdmsUnknownDeviceSerializer
    filterset_class = AdmsUnknownDeviceFilter
    search_fields = ["serial_number", "last_source_ip"]
    ordering_fields = ["last_seen_at", "request_count", "serial_number"]
    ordering = ["-last_seen_at"]

    def base_queryset(self):
        return AdmsUnknownDevice.objects.all()
