"""``leads/installations/`` — staff CRUD of customer installations (module ``leads``: view / create / edit / archive).

``is_showcase`` puts a COMPLETED installation on the public map (``/api/public/v1/installations/``); the stats
(``installations/stats/``) count every COMPLETED installation. Choosing someone else as assignee needs leads.manage.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from leads.filters import InstallationFilter
from leads.serializers.staff import InstallationCreateSerializer, InstallationSerializer, InstallationUpdateSerializer
from leads.services import installations

TAGS = ["leads"]
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="leads_installations_list", tags=TAGS),
    retrieve=extend_schema(operation_id="leads_installations_retrieve", responses={200: InstallationSerializer, **_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="leads_installations_create", request=InstallationCreateSerializer, responses={201: InstallationSerializer, **_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="leads_installations_update", request=InstallationUpdateSerializer, responses={200: InstallationSerializer, **_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="leads_installations_delete", responses={204: OpenApiResponse(description="Archived."), **_ERRORS}, tags=TAGS),
)
class InstallationViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "leads"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}
    services = {"create": installations.create_installation, "update": installations.update_installation, "destroy": installations.delete_installation}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = InstallationSerializer
    filterset_class = InstallationFilter
    search_fields = ["customer_name", "phone_e164", "pincode", "district"]
    ordering_fields = ["installed_on", "created_at", "capacity_kw"]
    ordering = ["-installed_on"]

    def base_queryset(self):
        return installations.installations_queryset()

    def get_serializer_class(self):
        return {"create": InstallationCreateSerializer, "partial_update": InstallationUpdateSerializer}.get(self.action, InstallationSerializer)
