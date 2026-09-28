from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from core.serializers import ErrorSerializer, FeatureFlagSerializer, FeatureFlagUpdateSerializer
from core.services.flags import list_flags, set_flag
from core.views.base import BaseAPIView


class FeatureFlagView(BaseAPIView):
    """``settings/flags/``: list every known flag; PATCH turns one on or off."""

    module = "settings"
    action_permissions = {"GET": "view", "PATCH": "edit"}

    @extend_schema(operation_id="settings_flags_list", responses={200: FeatureFlagSerializer(many=True)}, tags=["settings"])
    def get(self, request, *args, **kwargs):
        return Response([FeatureFlagSerializer.from_state(state) for state in list_flags()])

    @extend_schema(
        operation_id="settings_flags_update",
        request=FeatureFlagUpdateSerializer,
        responses={200: FeatureFlagSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer},
        tags=["settings"],
    )
    def patch(self, request, *args, **kwargs):
        serializer = FeatureFlagUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        set_flag(data["key"], enabled=data["enabled"], user=request.user, note=data.get("note"), expected_version=data.get("expected_version"))
        state = next(state for state in list_flags() if state["key"] == data["key"])
        return Response(FeatureFlagSerializer.from_state(state))
