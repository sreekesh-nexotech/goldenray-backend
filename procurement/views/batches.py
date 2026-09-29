"""``procurement/batches/`` — CRUD (DRAFT only editable; DELETE cancels a DRAFT), ``lines/`` and ``charges/`` (GET + bulk
PUT), ``preview-allocation/``, ``commit/``, ``reverse/`` (module ``procurement``: view · create · edit · commit).
Landed-cost fields need ``pricing_internal.view``."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.views.common import READ_ERRORS, UUID_REGEX, WRITE_ERRORS, InternalContextMixin, PatchOnlyUpdateMixin
from procurement.models import Batch, BatchCharge, BatchLine, BatchStatus
from procurement.serializers.shapes import (
    AllocationPreviewSerializer,
    BatchChargeSerializer,
    BatchChargesPutSerializer,
    BatchDetailSerializer,
    BatchLineSerializer,
    BatchLinesPutSerializer,
    BatchSerializer,
    BatchUpdateSerializer,
    BatchWriteSerializer,
    CommitSerializer,
    ReversalSerializer,
)
from procurement.services import allocation, batches

TAGS = ["procurement"]


class BatchFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=BatchStatus.choices)
    supplier = django_filters.UUIDFilter(field_name="supplier__uid")
    is_seed = django_filters.BooleanFilter()

    class Meta:
        model = Batch
        fields: list[str] = []


class ReverseSerializer(CommitSerializer):
    pass


@extend_schema_view(
    list=extend_schema(operation_id="procurement_batches_list", tags=TAGS),
    retrieve=extend_schema(operation_id="procurement_batches_retrieve", responses={200: BatchDetailSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="procurement_batches_create", request=BatchWriteSerializer, responses={201: BatchDetailSerializer, **WRITE_ERRORS}, tags=TAGS),
    update=extend_schema(exclude=True),
    partial_update=extend_schema(operation_id="procurement_batches_update", request=BatchUpdateSerializer, responses={200: BatchDetailSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="procurement_batches_cancel", responses={204: OpenApiResponse(description="DRAFT cancelled (status CANCELLED)."), **WRITE_ERRORS}, tags=TAGS),
)
class BatchViewSet(PatchOnlyUpdateMixin, InternalContextMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "procurement"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "lines": "view",
        "charges": "view",
        "preview_allocation": "view",
        "create": "create",
        "update": "edit",
        "partial_update": "edit",
        "destroy": "edit",
        "replace_lines": "edit",
        "replace_charges": "edit",
        "commit": "commit",
        "reverse": "commit",
    }
    services = {"create": batches.create_batch, "update": batches.update_batch, "destroy": batches.cancel_batch}
    http_method_names = ["get", "post", "put", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = BatchSerializer
    filterset_class = BatchFilter
    search_fields = ["number", "invoice_no", "supplier__name"]
    ordering_fields = ["created_at", "number", "committed_at"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return batches.batch_detail_queryset() if self.action in ("retrieve", "partial_update") else batches.batches_queryset()

    def get_serializer_class(self):
        return {"create": BatchWriteSerializer, "partial_update": BatchUpdateSerializer, "retrieve": BatchDetailSerializer}.get(self.action, BatchSerializer)

    def _detail(self, batch, status=200):
        return Response(BatchDetailSerializer(batches.batch_detail_queryset().get(pk=batch.pk), context=self.get_serializer_context()).data, status=status)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        serializer.instance = batches.batch_detail_queryset().get(pk=serializer.instance.pk)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        serializer.instance = batches.batch_detail_queryset().get(pk=serializer.instance.pk)

    def _body(self, serializer_class):
        body = serializer_class(data=self.request.data)
        body.is_valid(raise_exception=True)
        return body.validated_data

    @extend_schema(operation_id="procurement_batches_lines_list", responses={200: BatchLineSerializer(many=True), **READ_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["get"], filter_backends=[])
    def lines(self, request, *args, **kwargs):
        rows = BatchLine.objects.filter(batch=self.get_object()).select_related("component", "price_row").order_by("id")
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(BatchLineSerializer(page, many=True, context=self.get_serializer_context()).data)

    @extend_schema(operation_id="procurement_batches_lines_replace", request=BatchLinesPutSerializer, responses={200: BatchDetailSerializer, **WRITE_ERRORS}, tags=TAGS, description="DRAFT only.")
    @lines.mapping.put
    def replace_lines(self, request, *args, **kwargs):
        data = self._body(BatchLinesPutSerializer)
        batch = batches.put_lines(self.get_object(), user=request.user, rows=[dict(row) for row in data["lines"]], expected_version=data.get("expected_version"))
        return self._detail(batch)

    @extend_schema(operation_id="procurement_batches_charges_list", responses={200: BatchChargeSerializer(many=True), **READ_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["get"], filter_backends=[])
    def charges(self, request, *args, **kwargs):
        page = self.paginate_queryset(BatchCharge.objects.filter(batch=self.get_object()).order_by("id"))
        return self.get_paginated_response(BatchChargeSerializer(page, many=True).data)

    @extend_schema(
        operation_id="procurement_batches_charges_replace",
        request=BatchChargesPutSerializer,
        responses={200: BatchDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="DRAFT only. Charges (FREIGHT, INSURANCE, HANDLING, DUTY, OTHER) are supplier → warehouse and allocated by purchase value.",
    )
    @charges.mapping.put
    def replace_charges(self, request, *args, **kwargs):
        data = self._body(BatchChargesPutSerializer)
        batch = batches.put_charges(self.get_object(), user=request.user, rows=[dict(row) for row in data["charges"]], expected_version=data.get("expected_version"))
        return self._detail(batch)

    @extend_schema(
        operation_id="procurement_batches_preview_allocation",
        request=None,
        responses={200: AllocationPreviewSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Allocates the charges by purchase value (engines.cost.allocate_landed) without writing; lists the commit blockers.",
    )
    @action(detail=True, methods=["post"], url_path="preview-allocation", filter_backends=[])
    def preview_allocation(self, request, *args, **kwargs):
        return Response(AllocationPreviewSerializer(allocation.preview(self.get_object()), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="procurement_batches_commit",
        request=CommitSerializer,
        responses={200: BatchDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Writes PURCHASE + LANDED price rows per line (closing the previous ones) and makes the batch immutable.",
    )
    @action(detail=True, methods=["post"])
    def commit(self, request, *args, **kwargs):
        data = self._body(CommitSerializer)
        batch = allocation.commit(self.get_object(), user=request.user, reason=data["reason"], effective_from=data.get("effective_from"), expected_version=data.get("expected_version"))
        return self._detail(batch)

    @extend_schema(
        operation_id="procurement_batches_reverse",
        request=ReverseSerializer,
        responses={201: ReversalSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Creates the reversing batch (COMMITTED) and restores the prices the batch had superseded where it still sets them.",
    )
    @action(detail=True, methods=["post"])
    def reverse(self, request, *args, **kwargs):
        data = self._body(ReverseSerializer)
        reversal, outcome = allocation.reverse(self.get_object(), user=request.user, reason=data["reason"], effective_from=data.get("effective_from"), expected_version=data.get("expected_version"))
        body = {"reversal": batches.batch_detail_queryset().get(pk=reversal.pk), "lines": outcome}
        return Response(ReversalSerializer(body, context=self.get_serializer_context()).data, status=201)
