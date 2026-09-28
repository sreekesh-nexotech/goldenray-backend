"""``customers/`` — CRUD, ``merge/`` (customers.manage) and ``timeline/`` (module ``customers``, record scope owned/all)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from customers.filters import CustomerFilter
from customers.serializers.customers import (
    CustomerCreateSerializer,
    CustomerMergeSerializer,
    CustomerSerializer,
    CustomerUpdateSerializer,
    TimelineQuerySerializer,
    TimelineSerializer,
)
from customers.services import customers, merge, timeline

TAGS = ["customers"]
UUID_RE = "[0-9a-fA-F-]{36}"
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="customers_list", tags=TAGS),
    retrieve=extend_schema(operation_id="customers_retrieve", responses={200: CustomerSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(
        operation_id="customers_create",
        request=CustomerCreateSerializer,
        responses={201: CustomerSerializer, **_ERRORS},
        tags=TAGS,
        description="409 `phone_taken` when a live customer already has the number (its uid in `errors.existing_customer` when you may see it).",
    ),
    partial_update=extend_schema(operation_id="customers_update", request=CustomerUpdateSerializer, responses={200: CustomerSerializer, **_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="customers_delete",
        responses={204: OpenApiResponse(description="Archived (soft delete)."), **_ERRORS},
        tags=TAGS,
        description="409 `customer_in_use` while live quotations/agreements/… reference the customer (merge it instead).",
    ),
)
class CustomerViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "customers"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "merge": "manage",
        "timeline": "view",
    }
    services = {"create": customers.create_customer, "update": customers.update_customer, "destroy": customers.delete_customer}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_RE
    serializer_class = CustomerSerializer
    filterset_class = CustomerFilter
    search_fields = ["name", "phone_e164", "code", "email"]
    ordering_fields = ["name", "code", "created_at", "updated_at"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return customers.customers_queryset()

    def get_serializer_class(self):
        return {"create": CustomerCreateSerializer, "partial_update": CustomerUpdateSerializer}.get(self.action, CustomerSerializer)

    @extend_schema(
        operation_id="customers_merge",
        request=CustomerMergeSerializer,
        responses={200: CustomerSerializer, **_ERRORS},
        tags=TAGS,
        description=(
            "Merges this customer into `into_uid`: every registered reference (leads, notes, later quotations …) is re-pointed, the survivor's "
            "empty fields are filled, this record is archived with `merged_into`. Returns the survivor. Both customers must be visible to you."
        ),
    )
    @action(detail=True, methods=["post"], url_path="merge")
    def merge(self, request, *args, **kwargs):
        source = self.get_object()
        serializer = CustomerMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        target = self.get_queryset().filter(uid=data["into_uid"]).first()
        if target is None:
            raise NotFound(errors={"into_uid": ["No such customer."]})
        survivor = merge.merge_customers(source, into=target, user=request.user, expected_version=data.get("expected_version"), into_expected_version=data.get("into_expected_version"))
        return Response(CustomerSerializer(customers.customers_queryset().get(pk=survivor.pk)).data)

    @extend_schema(
        operation_id="customers_timeline",
        parameters=[
            OpenApiParameter("before", str, description="ISO date-time: only entries older than this (the previous page's `next_before`)."),
            OpenApiParameter("limit", int, description="Entries per page (1–200, default 50)."),
        ],
        responses={200: TimelineSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
        tags=TAGS,
        description="Newest first, across every context you may view (leads today; quotations, agreements, inspections and projects as they land).",
    )
    @action(detail=True, methods=["get"], url_path="timeline", filter_backends=[], pagination_class=None)
    def timeline(self, request, *args, **kwargs):
        customer = self.get_object()
        query = TimelineQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        entries, next_before = timeline.build(customer, user=request.user, before=query.validated_data.get("before"), limit=query.validated_data["limit"])
        return Response(TimelineSerializer({"results": entries, "next_before": next_before}).data)
