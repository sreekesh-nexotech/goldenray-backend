"""``quotations/`` — list/detail (record scope: Sales Executives see what they own), create, system options, revise,
accept, cancel, discount requests, history; the version sub-resources come from :mod:`quotations.views.versions`.

``quotations.view`` reads · ``create`` creates · ``edit`` edits drafts, accepts, cancels, asks for a discount ·
``issue`` issues, renders and sends · ``revise`` revises · ``approve`` decides discounts (never one's own).
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from quotations.models import DiscountRequest, Phase, Quotation, QuotationStatus, SystemType
from quotations.serializers.quotations import (
    CancelSerializer,
    DiscountCreateSerializer,
    DiscountRequestSerializer,
    HistorySerializer,
    QuotationCreateSerializer,
    QuotationDetailSerializer,
    QuotationSerializer,
    QuotationTransitionSerializer,
    ReviseSerializer,
    SystemOptionsSerializer,
    VersionDetailSerializer,
)
from quotations.services import lifecycle, options, quotations
from quotations.views.versions import READ_ERRORS, WRITE_ERRORS, VersionActionsMixin

TAGS = ["quotations"]


class QuotationFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=QuotationStatus.choices)
    customer = django_filters.UUIDFilter(field_name="customer__uid")
    owner = django_filters.UUIDFilter(field_name="owner__uid")
    legacy = django_filters.BooleanFilter()

    class Meta:
        model = Quotation
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="quotations_list", tags=TAGS),
    retrieve=extend_schema(operation_id="quotations_retrieve", responses={200: QuotationDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class QuotationViewSet(VersionActionsMixin, ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "quotations"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "system_options": "view",
        "version_detail": "view",
        "version_update": "edit",
        "preview": "view",
        "issue": "issue",
        "document": "view",
        "render": "issue",
        "send": "issue",
        "revise": "revise",
        "accept": "edit",
        "cancel": "edit",
        "discount_requests": "view",
        "request_discount": "edit",
        "approve_discount": "approve",
        "reject_discount": "approve",
        "history": "view",
    }
    http_method_names = ["get", "post", "patch"]
    serializer_class = QuotationSerializer
    filterset_class = QuotationFilter
    search_fields = ["number", "customer__name", "customer__code"]
    ordering_fields = ["created_at", "number", "valid_until", "status"]
    ordering = ["-created_at"]

    def base_queryset(self):
        queryset = quotations.quotations_queryset()
        if self.action == "retrieve":
            queryset = queryset.prefetch_related("versions")
        return queryset

    def get_serializer_class(self):
        return QuotationDetailSerializer if self.action == "retrieve" else QuotationSerializer

    def _detail(self, quotation, status=200):
        quotation = quotations.quotations_queryset().prefetch_related("versions").get(pk=quotation.pk)
        return Response(QuotationDetailSerializer(quotation, context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="quotations_create",
        request=QuotationCreateSerializer,
        responses={201: QuotationDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A quotation for a customer you can see, with its first DRAFT version pinned to the current PackRelease.",
    )
    def create(self, request, *args, **kwargs):
        body = QuotationCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(quotations.create(user=request.user, data=body.validated_data), status=201)

    @extend_schema(
        operation_id="quotations_system_options",
        parameters=[OpenApiParameter("system_type", str, enum=SystemType.values), OpenApiParameter("phase", str, enum=Phase.values)],
        responses={200: SystemOptionsSerializer, **READ_ERRORS, 409: ErrorSerializer},
        tags=TAGS,
        description="Sizes, tiers, phases, battery configurations and packs the current PackRelease can quote; roof types, vehicles, subsidy types, tier names.",
    )
    @action(detail=False, methods=["get"], url_path="system-options", filter_backends=[], pagination_class=None)
    def system_options(self, request, *args, **kwargs):
        system_type = request.query_params.get("system_type") if request.query_params.get("system_type") in SystemType.values else None
        phase = request.query_params.get("phase") if request.query_params.get("phase") in Phase.values else None
        return Response(SystemOptionsSerializer(options.system_options(system_type=system_type, phase=phase)).data)

    @extend_schema(
        operation_id="quotations_revise",
        request=ReviseSerializer,
        responses={201: VersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A new DRAFT version from the current releases (changes optional); the issued version becomes SUPERSEDED.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def revise(self, request, *args, **kwargs):
        body = ReviseSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        data.pop("refresh_release", None)
        return self._version_response(quotations.revise(self.get_object(), user=request.user, data=data, expected_version=expected), status=201)

    @extend_schema(
        operation_id="quotations_accept",
        request=QuotationTransitionSerializer,
        responses={200: QuotationDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="The customer accepted the issued, still valid quotation (emits quotations.accepted).",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def accept(self, request, *args, **kwargs):
        body = QuotationTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(lifecycle.accept(self.get_object(), user=request.user, note=body.validated_data.get("note", ""), expected_version=body.validated_data.get("expected_version")))

    @extend_schema(
        operation_id="quotations_cancel",
        request=CancelSerializer,
        responses={200: QuotationDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Cancel (lost) a draft, issued or expired quotation; the reason is kept.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def cancel(self, request, *args, **kwargs):
        body = CancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(lifecycle.cancel(self.get_object(), user=request.user, reason=body.validated_data["reason"], expected_version=body.validated_data.get("expected_version")))

    @extend_schema(
        operation_id="quotations_discount_requests_list",
        responses={200: DiscountRequestSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="Discount requests of every version (newest first).",
    )
    @action(detail=True, methods=["get"], url_path="discount-requests", filter_backends=[])
    def discount_requests(self, request, *args, **kwargs):
        queryset = DiscountRequest.objects.filter(quotation_version__quotation=self.get_object()).select_related("quotation_version", "requested_by", "decided_by").order_by("-created_at", "-id")
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(DiscountRequestSerializer(page, many=True).data)

    @extend_schema(
        operation_id="quotations_discount_requests_create",
        request=DiscountCreateSerializer,
        responses={201: DiscountRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Ask a Sales Head for a discount on the DRAFT version (one pending request at a time).",
    )
    @discount_requests.mapping.post
    def request_discount(self, request, *args, **kwargs):
        body = DiscountCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(DiscountRequestSerializer(lifecycle.request_discount(self.get_object(), user=request.user, **body.validated_data)).data, status=201)

    def _decide(self, request, request_uid, approve: bool):
        body = QuotationTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        discount = lifecycle.get_request(self.get_object(), request_uid)
        decided = lifecycle.decide_discount(discount, user=request.user, approve=approve, note=body.validated_data.get("note", ""), expected_version=body.validated_data.get("expected_version"))
        return Response(DiscountRequestSerializer(decided).data)

    @extend_schema(
        operation_id="quotations_discount_requests_approve",
        request=QuotationTransitionSerializer,
        responses={200: DiscountRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Approve (the amount is printed at issue, D-4); never your own request.",
    )
    @action(detail=True, methods=["post"], url_path=r"discount-requests/(?P<request_uid>[0-9a-f-]{36})/approve", filter_backends=[], pagination_class=None)
    def approve_discount(self, request, request_uid, *args, **kwargs):
        return self._decide(request, request_uid, True)

    @extend_schema(
        operation_id="quotations_discount_requests_reject",
        request=QuotationTransitionSerializer,
        responses={200: DiscountRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Reject a pending discount request; never your own.",
    )
    @action(detail=True, methods=["post"], url_path=r"discount-requests/(?P<request_uid>[0-9a-f-]{36})/reject", filter_backends=[], pagination_class=None)
    def reject_discount(self, request, request_uid, *args, **kwargs):
        return self._decide(request, request_uid, False)

    @extend_schema(
        operation_id="quotations_history", responses={200: HistorySerializer, **READ_ERRORS}, tags=TAGS, description="Every version and the quotation's audit trail (newest first, at most 200 events)."
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def history(self, request, *args, **kwargs):
        return Response(HistorySerializer(lifecycle.history(self.get_object())).data)
