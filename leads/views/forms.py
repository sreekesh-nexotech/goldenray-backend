"""``leads/affiliate-applications/`` and ``leads/warranty-requests/`` (staff, module ``leads``).

List/detail (view), PATCH (edit), DELETE (archive), POST ``…/transition/`` (edit), POST ``…/assign/`` (manage).
Records are created only by the website forms (``/api/public/v1/affiliate-applications/``, ``warranty-requests/``).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from customers.services.customers import visible_customers
from leads.filters import AffiliateFilter, WarrantyFilter
from leads.serializers.staff import (
    AffiliateSerializer,
    AffiliateTransitionSerializer,
    AffiliateUpdateSerializer,
    AssignSerializer,
    WarrantySerializer,
    WarrantyTransitionSerializer,
    WarrantyUpdateSerializer,
)
from leads.services import workflows

UUID_RE = "[0-9a-fA-F-]{36}"
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
_PERMISSIONS = {"list": "view", "retrieve": "view", "partial_update": "edit", "destroy": "archive", "transition": "edit", "assign": "manage"}
_SERVICES = {"update": workflows.update_record, "destroy": workflows.delete_record}


class _WorkflowViewSet(ListModelMixin, RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "leads"
    action_permissions = _PERMISSIONS
    services = _SERVICES
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_RE
    ordering_fields = ["created_at", "updated_at", "status"]
    ordering = ["-created_at"]
    read_serializer = None
    update_serializer = None
    transition_serializer = None

    def get_serializer_class(self):
        return self.update_serializer if self.action == "partial_update" else self.read_serializer

    def _respond(self, row):
        return Response(self.read_serializer(self.base_queryset().get(pk=row.pk)).data)

    def _transition(self, request):
        row = self.get_object()
        serializer = self.transition_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return self._respond(workflows.transition(row, user=request.user, status=data["status"], note=data.get("note", ""), expected_version=data.get("expected_version")))

    def _assign(self, request):
        row = self.get_object()
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return self._respond(workflows.assign(row, user=request.user, assignee=data["assignee"], expected_version=data.get("expected_version")))


@extend_schema_view(
    list=extend_schema(operation_id="leads_affiliate_applications_list", tags=["leads"]),
    retrieve=extend_schema(operation_id="leads_affiliate_applications_retrieve", responses={200: AffiliateSerializer, **_ERRORS}, tags=["leads"]),
    partial_update=extend_schema(operation_id="leads_affiliate_applications_update", request=AffiliateUpdateSerializer, responses={200: AffiliateSerializer, **_ERRORS}, tags=["leads"]),
    destroy=extend_schema(operation_id="leads_affiliate_applications_delete", responses={204: OpenApiResponse(description="Archived."), **_ERRORS}, tags=["leads"]),
)
class AffiliateApplicationViewSet(_WorkflowViewSet):
    filterset_class = AffiliateFilter
    search_fields = ["full_name", "phone_e164", "email"]
    serializer_class = AffiliateSerializer
    read_serializer = AffiliateSerializer
    update_serializer = AffiliateUpdateSerializer
    transition_serializer = AffiliateTransitionSerializer

    def base_queryset(self):
        return workflows.affiliate_queryset()

    @extend_schema(
        operation_id="leads_affiliate_applications_transition",
        request=AffiliateTransitionSerializer,
        responses={200: AffiliateSerializer, **_ERRORS},
        tags=["leads"],
        description="NEW → CONTACTED → APPROVED | REJECTED (NEW may go straight to either); REJECTED → NEW. 409 `invalid_transition`.",
    )
    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, *args, **kwargs):
        return self._transition(request)

    @extend_schema(operation_id="leads_affiliate_applications_assign", request=AssignSerializer, responses={200: AffiliateSerializer, **_ERRORS}, tags=["leads"])
    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, *args, **kwargs):
        return self._assign(request)


@extend_schema_view(
    list=extend_schema(operation_id="leads_warranty_requests_list", tags=["leads"]),
    retrieve=extend_schema(operation_id="leads_warranty_requests_retrieve", responses={200: WarrantySerializer, **_ERRORS}, tags=["leads"]),
    partial_update=extend_schema(operation_id="leads_warranty_requests_update", request=WarrantyUpdateSerializer, responses={200: WarrantySerializer, **_ERRORS}, tags=["leads"]),
    destroy=extend_schema(operation_id="leads_warranty_requests_delete", responses={204: OpenApiResponse(description="Archived."), **_ERRORS}, tags=["leads"]),
)
class WarrantyRequestViewSet(_WorkflowViewSet):
    filterset_class = WarrantyFilter
    search_fields = ["full_name", "phone_e164", "description"]
    serializer_class = WarrantySerializer
    read_serializer = WarrantySerializer
    update_serializer = WarrantyUpdateSerializer
    transition_serializer = WarrantyTransitionSerializer

    def base_queryset(self):
        return workflows.warranty_queryset()

    def perform_update(self, serializer):
        # ``customer_uid`` → a customer the user may see (customers record scope), else 404.
        uid = serializer.validated_data.pop("customer_uid", "unset")
        if uid != "unset":
            customer = visible_customers(self.request.user).filter(uid=uid).first() if uid else None
            if uid and customer is None:
                raise NotFound(errors={"customer_uid": ["No such customer."]})
            serializer.validated_data["customer"] = customer
        super().perform_update(serializer)

    @extend_schema(
        operation_id="leads_warranty_requests_transition",
        request=WarrantyTransitionSerializer,
        responses={200: WarrantySerializer, **_ERRORS},
        tags=["leads"],
        description="NEW → IN_PROGRESS → RESOLVED → CLOSED; NEW/IN_PROGRESS → REJECTED; RESOLVED → IN_PROGRESS; REJECTED → NEW. 409 `invalid_transition`.",
    )
    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, *args, **kwargs):
        return self._transition(request)

    @extend_schema(operation_id="leads_warranty_requests_assign", request=AssignSerializer, responses={200: WarrantySerializer, **_ERRORS}, tags=["leads"])
    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, *args, **kwargs):
        return self._assign(request)
