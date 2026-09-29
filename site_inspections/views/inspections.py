"""``site-inspections/`` (staff, module ``site_inspections``; record scope all / owned / assigned) and the engineer
queue ``engineer/site-inspections/``. Actions live in the ``workflow``, ``records`` and ``decisions`` mixins."""

from __future__ import annotations

from django.db.models import Case, IntegerField, Value, When
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status as http_status
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin
from customers.services.customers import visible_customers
from site_inspections.filters import InspectionFilter
from site_inspections.models.choices import Status
from site_inspections.serializers.inspections import InspectionCreateSerializer, InspectionListSerializer, InspectionSerializer
from site_inspections.services import common, inspections
from site_inspections.views.decisions import DECISION_PERMISSIONS, DecisionActionsMixin
from site_inspections.views.records import RECORD_PERMISSIONS, RecordActionsMixin
from site_inspections.views.workflow import WORKFLOW_PERMISSIONS, WorkflowActionsMixin

TAGS = ["site-inspections"]
UID = "[0-9a-fA-F-]{36}"
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="site_inspections_list", tags=TAGS, description="Scoped list (all / owned / assigned). No prices."),
    retrieve=extend_schema(operation_id="site_inspections_retrieve", responses={200: InspectionSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(
        operation_id="site_inspections_create",
        request=InspectionCreateSerializer,
        responses={201: InspectionSerializer, **ERRORS},
        tags=TAGS,
        description="PRE_SALE only; agreement inspections come from `agreements.issued`.",
    ),
    destroy=extend_schema(operation_id="site_inspections_archive", responses={204: OpenApiResponse(description="Archived (soft delete)."), **ERRORS}, tags=TAGS),
)
class InspectionViewSet(WorkflowActionsMixin, RecordActionsMixin, DecisionActionsMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, DestroyModelMixin, BaseViewSet):
    module = common.MODULE
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "destroy": "archive", **WORKFLOW_PERMISSIONS, **RECORD_PERMISSIONS, **DECISION_PERMISSIONS}
    services = {"create": inspections.create_inspection, "destroy": inspections.archive}
    http_method_names = ["get", "post", "patch", "put", "delete"]
    lookup_value_regex = UID
    serializer_class = InspectionSerializer
    filterset_class = InspectionFilter
    search_fields = ["number", "customer__name", "customer__phone_e164", "address", "agreement_number"]
    ordering_fields = ["created_at", "updated_at", "visit_date", "status", "number"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return common.inspections_queryset()

    def get_serializer_class(self):
        return {"list": InspectionListSerializer, "create": InspectionCreateSerializer}.get(self.action, InspectionSerializer)

    def create(self, request, *args, **kwargs):
        serializer = InspectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        customer = visible_customers(request.user).filter(uid=data.pop("customer_uid")).first()
        if customer is None:
            raise NotFound("customer_not_found", "No such customer.", errors={"customer_uid": ["No such customer."]})
        inspection = self.get_service("create")(user=request.user, data={**data, "customer": customer})
        return Response(InspectionSerializer(self.base_queryset().get(pk=inspection.pk), context=self.get_serializer_context()).data, status=http_status.HTTP_201_CREATED)


QUEUE_ORDER = Case(
    When(status=Status.IN_PROGRESS, then=Value(1)),
    When(status=Status.DRAFT, then=Value(2)),
    When(status=Status.REVISION_REQUIRED, then=Value(2)),
    When(status=Status.CUSTOMER_APPROVAL_PENDING, then=Value(3)),
    When(status=Status.COMPLETED, then=Value(4)),
    default=Value(5),
    output_field=IntegerField(),
)


@extend_schema_view(list=extend_schema(operation_id="engineer_site_inspections_list", tags=TAGS, description="The caller's own assigned inspections (work first, then by visit date)."))
class EngineerQueueViewSet(ListModelMixin, BaseViewSet):
    module = common.MODULE
    action_permissions = {"list": "view"}
    serializer_class = InspectionListSerializer
    filterset_class = InspectionFilter
    search_fields = ["number", "customer__name", "address"]
    ordering_fields = []

    def base_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return common.inspections_queryset().none()
        return (
            common.inspections_queryset()
            .filter(engineer=self.request.user)
            .exclude(status=Status.INSTALLATION_READY)
            .annotate(queue_rank=QUEUE_ORDER)
            .order_by("queue_rank", "visit_date", "-created_at")
        )
