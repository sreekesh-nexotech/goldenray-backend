"""The office-agent protocol (``/api/agent/<version>/``): ``Authorization: Bearer fl_…`` bound to an active agent.

``GET config/`` · ``POST heartbeat/`` · ``POST devices/announce/`` · ``POST devices/identity-mismatch/`` ·
``POST devices/discovery/`` · ``POST sync/users/`` · ``POST sync/attendance/`` (≤ 200 punches) · ``GET sync-status/``.
Uploads honour ``Idempotency-Key`` (a retried request replays the first answer); every call is throttled per token
(scope ``agent``).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from core.idempotency import idempotent
from core.serializers import ErrorSerializer
from core.views import AgentAPIView
from devices.authentication import AgentTokenAuthentication, agent_of
from devices.serializers.agent_protocol import (
    AgentConfigSerializer,
    AnnounceResultSerializer,
    AnnounceSerializer,
    AttendanceResultSerializer,
    DiscoveryResultSerializer,
    DiscoverySerializer,
    HeartbeatSerializer,
    IdentityMismatchResultSerializer,
    IdentityMismatchSerializer,
    SyncAttendanceSerializer,
    SyncStatusQuerySerializer,
    SyncStatusSerializer,
    SyncUsersSerializer,
    UsersResultSerializer,
)
from devices.services import agent_protocol

TAGS = ["agent"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer}
DEVICE_ERRORS = {**ERRORS, 404: ErrorSerializer}
IDEMPOTENCY = OpenApiParameter(
    "Idempotency-Key",
    str,
    OpenApiParameter.HEADER,
    required=False,
    description="8–128 characters; a retry with the same key and body replays the first answer for 24 h (a different body: 422 `idempotency_key_reused`).",
)


class AgentProtocolView(AgentAPIView):
    authentication_classes = [AgentTokenAuthentication]

    @property
    def agent(self):
        return agent_of(self.request)

    def body(self, serializer_class):
        serializer = serializer_class(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class ConfigView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_config",
        responses={200: AgentConfigSerializer, 401: ErrorSerializer, 403: ErrorSerializer},
        tags=TAGS,
        description="Identity, intervals and the terminals this agent is responsible for.",
    )
    def get(self, request, *args, **kwargs):
        return Response(AgentConfigSerializer(agent_protocol.agent_config(self.agent)).data)


class HeartbeatView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_heartbeat",
        request=HeartbeatSerializer,
        responses={200: AgentConfigSerializer, **ERRORS},
        tags=TAGS,
        description="Liveness plus the truth about each terminal's LAN reachability; answered with the configuration.",
    )
    def post(self, request, *args, **kwargs):
        return Response(AgentConfigSerializer(agent_protocol.heartbeat(self.agent, self.body(HeartbeatSerializer))).data)


class AnnounceView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_devices_announce",
        request=AnnounceSerializer,
        responses={200: AnnounceResultSerializer, **ERRORS, 409: ErrorSerializer},
        tags=TAGS,
        description=(
            "Bind or refresh a terminal by the serial it reports. 409 `device_bound_elsewhere` (another agent's or another office's device — "
            "re-homing is a staff action), `identity_mismatch`, `device_inactive`."
        ),
    )
    def post(self, request, *args, **kwargs):
        device, created = agent_protocol.announce(self.agent, self.body(AnnounceSerializer))
        return Response(AnnounceResultSerializer({"uid": device.uid, "name": device.name, "serial_number": device.serial_number, "office": device.office, "created": created}).data)


class IdentityMismatchView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_devices_identity_mismatch",
        request=IdentityMismatchSerializer,
        responses={200: IdentityMismatchResultSerializer, **ERRORS},
        tags=TAGS,
        description="Evidence that a terminal answered as another device; never changes the stored identity.",
    )
    def post(self, request, *args, **kwargs):
        return Response(IdentityMismatchResultSerializer(agent_protocol.report_identity_mismatch(self.agent, self.body(IdentityMismatchSerializer))).data)


class DiscoveryView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_devices_discovery",
        request=DiscoverySerializer,
        responses={200: DiscoveryResultSerializer, **ERRORS},
        tags=TAGS,
        description="A LAN scan: locates this agent's terminals by the serial they state; decides no identity.",
    )
    def post(self, request, *args, **kwargs):
        return Response(DiscoveryResultSerializer(agent_protocol.record_discovery(self.agent, self.body(DiscoverySerializer))).data)


class SyncUsersView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_sync_users",
        parameters=[IDEMPOTENCY],
        request=SyncUsersSerializer,
        responses={200: UsersResultSerializer, **DEVICE_ERRORS, 409: ErrorSerializer, 422: ErrorSerializer},
        tags=TAGS,
        description="The terminal's whole user table (upsert; the read, dated `read_at`, is the presence watermark). 409 `device_inactive`.",
    )
    @idempotent("devices.agent_sync_users")
    def post(self, request, *args, **kwargs):
        data = self.body(SyncUsersSerializer)
        device = agent_protocol.resolve_device(self.agent, data.get("device"), data.get("serial_number"))
        result = agent_protocol.sync_users(self.agent, device, data["users"], read_at=data.get("read_at"))
        return Response(UsersResultSerializer({"device": device.uid, **result}).data)


class SyncAttendanceView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_sync_attendance",
        parameters=[IDEMPOTENCY],
        request=SyncAttendanceSerializer,
        responses={200: AttendanceResultSerializer, **DEVICE_ERRORS, 409: ErrorSerializer, 422: ErrorSerializer},
        tags=TAGS,
        description="A batch of at most 200 punches; idempotent (content dedup key + Idempotency-Key). A resend reports duplicates and stores nothing twice. 409 `device_inactive`.",
    )
    @idempotent("devices.agent_sync_attendance")
    def post(self, request, *args, **kwargs):
        data = self.body(SyncAttendanceSerializer)
        device = agent_protocol.resolve_device(self.agent, data.get("device"), data.get("serial_number"))
        result = agent_protocol.sync_attendance(self.agent, device, data["records"], batch_id=data.get("batch_id", ""))
        return Response(AttendanceResultSerializer({"device": device.uid, **result}).data)


class SyncStatusView(AgentProtocolView):
    @extend_schema(
        operation_id="agent_sync_status",
        parameters=[SyncStatusQuerySerializer],
        responses={200: SyncStatusSerializer, **DEVICE_ERRORS},
        tags=TAGS,
        description="What the server already holds for one terminal (a resume hint; dedup decides).",
    )
    def get(self, request, *args, **kwargs):
        query = SyncStatusQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        device = agent_protocol.resolve_device(self.agent, query.validated_data.get("device"), query.validated_data.get("serial_number"))
        return Response(SyncStatusSerializer(agent_protocol.sync_status(device)).data)
