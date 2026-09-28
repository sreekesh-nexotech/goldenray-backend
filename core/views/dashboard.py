from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from core.dashboard import counters_for
from core.serializers import DashboardSerializer, ErrorSerializer
from core.views.base import BaseAPIView


class DashboardView(BaseAPIView):
    """``dashboard/``: counters for every module the user can view (each counter applies record scope)."""

    module = "dashboard"
    action_permissions = {"GET": "view"}

    @extend_schema(operation_id="dashboard_retrieve", responses={200: DashboardSerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=["dashboard"])
    def get(self, request, *args, **kwargs):
        return Response({"modules": counters_for(request.user)})
