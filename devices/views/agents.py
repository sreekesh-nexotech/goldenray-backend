"""``devices/agents/`` — office agents (module ``devices``).

view: list, detail, ``…/logs/`` · edit: ``PATCH`` (name, office, intervals, thresholds, settings, notes) · manage:
create (issues the token, shown once), ``…/rotate-token/``, ``…/revoke/``, ``…/config-download/``, ``DELETE``.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Prefetch
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from devices.filters import AgentFilter
from devices.models import Device, SyncLog
from devices.serializers.agents import AgentCreateSerializer, AgentSerializer, AgentTokenSerializer, AgentUpdateSerializer, ConfigDownloadQuerySerializer
from devices.serializers.devices import ActionSerializer
from devices.serializers.refs import SyncLogSerializer
from devices.services import agents
from devices.views.common import DELETED, LOG_PARAMETERS, READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, MomentMixin, SyncLogCursorPagination, paginate_logs

TOKEN_WARNING = "Store this now: only a hash is kept, so it cannot be shown again. Rotating issues a new token and immediately invalidates this one."


@extend_schema_view(
    list=extend_schema(operation_id="devices_agents_list", tags=TAGS),
    retrieve=extend_schema(operation_id="devices_agents_retrieve", responses={200: AgentSerializer, **READ_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="devices_agents_update", request=AgentUpdateSerializer, responses={200: AgentSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="devices_agents_delete",
        responses={204: DELETED, **WRITE_ERRORS},
        tags=TAGS,
        description="Soft delete (its credential is revoked); 409 `agent_in_use` while devices are bound to it.",
    ),
)
class AgentViewSet(MomentMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "devices"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "logs": "view",
        "partial_update": "edit",
        "create": "manage",
        "rotate_token": "manage",
        "revoke": "manage",
        "config_download": "manage",
        "destroy": "manage",
    }
    services = {"update": agents.update_agent, "destroy": agents.delete_agent}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = AgentSerializer
    filterset_class = AgentFilter
    search_fields = ["code", "name", "hostname"]
    ordering_fields = ["code", "name", "last_heartbeat_at", "created_at"]
    ordering = ["code"]

    def get_serializer_class(self):
        return AgentUpdateSerializer if self.action == "partial_update" else AgentSerializer

    def base_queryset(self):
        return agents.agents_queryset().prefetch_related(Prefetch("devices", queryset=Device.objects.select_related("office").order_by("name", "id")))

    def _token_response(self, agent, token, download, status=200):
        agent = self.base_queryset().get(pk=agent.pk)
        payload = {"agent": agent, "token": token, "config_download": download, "warning": TOKEN_WARNING}
        return Response(AgentTokenSerializer(payload, context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="devices_agents_create",
        request=AgentCreateSerializer,
        responses={201: AgentTokenSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Creates the agent and its credential; the token is returned once (409 `agent_code_taken`).",
    )
    def create(self, request, *args, **kwargs):
        body = AgentCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        agent, token, download = agents.create_agent(user=request.user, data=body.validated_data)
        return self._token_response(agent, token, download, status=201)

    @extend_schema(
        operation_id="devices_agents_rotate_token",
        request=ActionSerializer,
        responses={200: AgentTokenSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="New token (shown once); the old one stops working at once. Re-activates a revoked agent with a fresh credential.",
    )
    @action(detail=True, methods=["post"], url_path="rotate-token")
    def rotate_token(self, request, *args, **kwargs):
        body = ActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        agent, token, download = agents.rotate_token(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"))
        return self._token_response(agent, token, download)

    @extend_schema(
        operation_id="devices_agents_revoke",
        request=ActionSerializer,
        responses={200: AgentSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Withdraw the credential and disable the agent (devices, logs and punches stay).",
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, *args, **kwargs):
        body = ActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        agents.revoke_agent(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"))
        return Response(AgentSerializer(self.base_queryset().get(pk=self.get_object().pk), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_agents_config_download",
        parameters=[OpenApiParameter("download", OpenApiTypes.STR, required=True, description="The single-use key from the create/rotate response.")],
        responses={(200, "text/plain"): OpenApiResponse(OpenApiTypes.STR, description="agent.ini"), 400: ErrorSerializer, **READ_ERRORS, 410: ErrorSerializer},
        tags=TAGS,
        description="agent.ini with the token, exactly once and within 10 minutes of the create/rotate (410 `download_expired`).",
    )
    @action(detail=True, methods=["get"], url_path="config-download")
    def config_download(self, request, *args, **kwargs):
        query = ConfigDownloadQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        agent = self.get_object()
        token = agents.take_config_token(agent, query.validated_data["download"])
        server_url = getattr(settings, "DEVICES_AGENT_SERVER_URL", "") or request.build_absolute_uri("/").rstrip("/")
        response = HttpResponse(agents.render_agent_ini(agent, token, server_url), content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="agent-{agent.code}.ini"'
        response["Cache-Control"] = "private, no-store"
        return response

    @extend_schema(
        operation_id="devices_agents_logs",
        parameters=LOG_PARAMETERS,
        responses={200: SyncLogSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="Sync logs written for this agent, newest first (cursor).",
    )
    @action(detail=True, methods=["get"], pagination_class=SyncLogCursorPagination)
    def logs(self, request, *args, **kwargs):
        agent = self.get_object()
        queryset = SyncLog.objects.filter(agent=agent).select_related("device__office", "agent")
        for name in ("sync_type", "status"):
            if request.query_params.get(name):
                queryset = queryset.filter(**{name: request.query_params[name]})
        return paginate_logs(self, queryset, SyncLogSerializer)
