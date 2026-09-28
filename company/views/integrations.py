"""``settings/integrations/`` — Twilio, Bunny and SMTP settings (module ``settings``: view / edit; secrets write-only)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from company.serializers.integrations import IntegrationPutSerializer, IntegrationSerializer
from company.services import integrations
from core.serializers import ErrorSerializer
from core.views import BaseAPIView

TAGS = ["settings"]


class IntegrationsView(BaseAPIView):
    module = "settings"
    action_permissions = {"GET": "view", "PUT": "edit"}

    @extend_schema(operation_id="settings_integrations_list", responses={200: IntegrationSerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        return Response(IntegrationSerializer(integrations.list_integrations(), many=True).data)

    @extend_schema(
        operation_id="settings_integrations_put",
        request=IntegrationPutSerializer,
        responses={200: IntegrationSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 409: ErrorSerializer},
        tags=TAGS,
        description="Replaces one integration's settings. Secret fields: omit to keep the stored value, send a string to replace it, null or '' to clear it.",
    )
    def put(self, request, *args, **kwargs):
        serializer = IntegrationPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = integrations.put_integration(user=request.user, key=data["key"], is_enabled=data["is_enabled"], config=data["config"], expected_version=data.get("expected_version"))
        return Response(IntegrationSerializer(result).data)
