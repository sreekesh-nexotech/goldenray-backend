"""``change-requests/`` (staff, module ``change_requests``; record scope all / owned = asked by me).

``create`` submits (``change_requests.create``), ``approve/`` applies and ``reject/`` declines
(``change_requests.approve``), ``cancel/`` withdraws one's own (``change_requests.create``); ``kinds/`` lists what
can be requested.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from approvals.models import ChangeRequest
from approvals.serializers.requests import ChangeKindSerializer, ChangeRequestCreateSerializer, ChangeRequestDecisionSerializer, ChangeRequestSerializer
from approvals.services import kinds, requests
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin

TAGS = ["change-requests"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class ChangeRequestFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=ChangeRequest.Status.choices)
    kind = django_filters.CharFilter()
    operation = django_filters.ChoiceFilter(choices=ChangeRequest.Operation.choices)
    requested_by = django_filters.UUIDFilter(field_name="created_by__uid")

    class Meta:
        model = ChangeRequest
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="change_requests_list", tags=TAGS),
    retrieve=extend_schema(operation_id="change_requests_retrieve", responses={200: ChangeRequestSerializer, **READ_ERRORS}, tags=TAGS),
)
class ChangeRequestViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "change_requests"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "kinds": "view",
        "create": "create",
        "cancel": "create",
        "approve": "approve",
        "reject": "approve",
    }
    http_method_names = ["get", "post"]
    lookup_value_regex = UUID_REGEX
    serializer_class = ChangeRequestSerializer
    filterset_class = ChangeRequestFilter
    search_fields = ["target_label", "reason", "kind"]
    ordering_fields = ["created_at", "decided_at", "status"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return requests.requests_queryset()

    def _context(self):
        return {"request": self.request}

    def _detail(self, change, status=200):
        return Response(ChangeRequestSerializer(requests.requests_queryset().get(pk=change.pk)).data, status=status)

    @extend_schema(
        operation_id="change_requests_create",
        request=ChangeRequestCreateSerializer,
        responses={201: ChangeRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Ask an Admin to apply a change. The payload is validated now (400 as the direct endpoint would); nothing changes until it is approved.",
    )
    def create(self, request, *args, **kwargs):
        body = ChangeRequestCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        change = requests.submit(
            user=request.user,
            kind=data["kind"],
            operation=data["operation"],
            target_uid=data.get("target_uid"),
            payload=data.get("payload"),
            reason=data.get("reason", ""),
            context=self._context(),
        )
        return self._detail(change, status=http_status.HTTP_201_CREATED)

    def _decision(self, request):
        body = ChangeRequestDecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return body.validated_data

    @extend_schema(
        operation_id="change_requests_approve",
        request=ChangeRequestDecisionSerializer,
        responses={200: ChangeRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Applies the change as you, then marks it approved. 403 `self_approval`; 409 `stale_version` when the row changed since the request.",
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        data = self._decision(request)
        change = requests.approve(self.get_object(), user=request.user, note=data.get("note", ""), expected_version=data.get("expected_version"), context=self._context())
        return self._detail(change)

    @extend_schema(
        operation_id="change_requests_reject",
        request=ChangeRequestDecisionSerializer,
        responses={200: ChangeRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Declines it; `note` is required.",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, *args, **kwargs):
        data = self._decision(request)
        return self._detail(requests.reject(self.get_object(), user=request.user, note=data.get("note", ""), expected_version=data.get("expected_version")))

    @extend_schema(
        operation_id="change_requests_cancel",
        request=ChangeRequestDecisionSerializer,
        responses={200: ChangeRequestSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Withdraw your own pending request.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, *args, **kwargs):
        data = self._decision(request)
        return self._detail(requests.cancel(self.get_object(), user=request.user, expected_version=data.get("expected_version")))

    @extend_schema(operation_id="change_requests_kinds", responses={200: ChangeKindSerializer(many=True), **READ_ERRORS}, tags=TAGS, description="What can be requested.")
    @action(detail=False, methods=["get"], filter_backends=[], pagination_class=None)
    def kinds(self, request, *args, **kwargs):
        rows = [{"key": k.key, "label": k.label, "module": k.module, "operations": list(k.operations)} for k in kinds.registered()]
        return Response(ChangeKindSerializer(rows, many=True).data)
