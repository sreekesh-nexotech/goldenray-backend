"""``packs/config-versions/`` — list/detail, new draft, PATCH config, packs, pins, run-checker, submit/approve/reject.

``packs.view`` reads · ``packs.edit`` creates drafts, edits config and pins · ``packs.submit`` · ``packs.approve``
(approve, reject) · ``engineering.verify`` runs the checker.
"""

from __future__ import annotations

import django_filters
from django.db.models import BooleanField, ExpressionWrapper, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from engineering.serializers import RunDetailSerializer
from packs.models import ConfigPack, ConfigStatus, ConfigVersion, SystemType, Tier
from packs.serializers.versions import (
    ApproveSerializer,
    ConfigPackSerializer,
    ConfigVersionCreateSerializer,
    ConfigVersionDetailSerializer,
    ConfigVersionSerializer,
    ConfigVersionUpdateSerializer,
    PinsSerializer,
    RejectSerializer,
    TransitionSerializer,
)
from packs.services import versions

TAGS = ["packs"]
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
PACK_KEY_REGEX = "[a-z0-9][a-z0-9.-]{0,63}"


class ConfigVersionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ConfigStatus.choices)

    class Meta:
        model = ConfigVersion
        fields: list[str] = []


class PackFilter(django_filters.FilterSet):
    system_type = django_filters.ChoiceFilter(choices=SystemType.choices)
    tier = django_filters.ChoiceFilter(choices=Tier.choices)

    class Meta:
        model = ConfigPack
        fields: list[str] = []


def _transition(name: str, request_serializer, description: str):
    return extend_schema(operation_id=f"packs_config_versions_{name}", request=request_serializer, responses={200: ConfigVersionDetailSerializer, **WRITE_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="packs_config_versions_list", tags=TAGS),
    retrieve=extend_schema(operation_id="packs_config_versions_retrieve", responses={200: ConfigVersionDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class ConfigVersionViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "packs"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "edit",
        "partial_update": "edit",
        "packs": "view",
        "pack": "view",
        "set_pins": "edit",
        "run_checker": ("engineering", "verify"),
        "submit": "submit",
        "approve": "approve",
        "reject": "approve",
    }
    http_method_names = ["get", "post", "patch", "put"]
    serializer_class = ConfigVersionSerializer
    filterset_class = ConfigVersionFilter
    search_fields = ["note"]
    ordering_fields = ["number", "created_at"]
    ordering = ["-number"]

    def base_queryset(self):
        queryset = versions.versions_queryset()
        if self.action == "retrieve":
            return queryset
        return queryset.defer("config", "change_log").annotate(config_present=ExpressionWrapper(Q(config__isnull=False), output_field=BooleanField()))

    def get_serializer_class(self):
        return ConfigVersionDetailSerializer if self.action == "retrieve" else ConfigVersionSerializer

    def _detail(self, version, status=200):
        return Response(ConfigVersionDetailSerializer(version, context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="packs_config_versions_create",
        request=ConfigVersionCreateSerializer,
        responses={201: ConfigVersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A new DRAFT copied from the current approved version (or `based_on_uid`); 409 `draft_exists` while one is open.",
    )
    def create(self, request, *args, **kwargs):
        body = ConfigVersionCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(versions.create_draft(user=request.user, data=body.validated_data), status=201)

    @extend_schema(
        operation_id="packs_config_versions_partial_update",
        request=ConfigVersionUpdateSerializer,
        responses={200: ConfigVersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Edit the open draft: the whole `config` or some `sections` (validated by engines.pack_config); a SUBMITTED draft goes back to DRAFT.",
    )
    def partial_update(self, request, *args, **kwargs):
        version = self.get_object()
        body = ConfigVersionUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        return self._detail(versions.update_draft(version, user=request.user, data=data, expected_version=expected))

    @extend_schema(
        operation_id="packs_config_versions_packs",
        parameters=[OpenApiParameter("system_type", str, enum=SystemType.values), OpenApiParameter("tier", str, enum=Tier.values)],
        responses={200: ConfigPackSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="The typed packs of the version (mirrored from its configuration) with their pins and BOM lines.",
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def packs(self, request, *args, **kwargs):
        version = self.get_object()
        queryset = (
            ConfigPack.objects.filter(config_version=version).select_related("panel", "inverter", "battery", "pair_of").prefetch_related("pins__component", "lines__component").order_by("sort_order")
        )
        queryset = PackFilter(request.query_params, queryset=queryset).qs
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(ConfigPackSerializer(page, many=True).data)

    @extend_schema(operation_id="packs_config_versions_pack_retrieve", responses={200: ConfigPackSerializer, **READ_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path=rf"packs/(?P<key>{PACK_KEY_REGEX})", filter_backends=[], pagination_class=None)
    def pack(self, request, *args, **kwargs):
        return Response(ConfigPackSerializer(versions.get_pack(self.get_object(), kwargs["key"])).data)

    @extend_schema(
        operation_id="packs_config_versions_pack_update",
        request=PinsSerializer,
        responses={200: ConfigPackSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Replace the pack's pinned components ({slot: component uid}); each must be selectable and of the slot's category.",
    )
    @pack.mapping.put
    def set_pins(self, request, *args, **kwargs):
        version = self.get_object()
        body = PinsSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        pack = versions.set_pins(
            version,
            kwargs["key"],
            user=request.user,
            pins={k: v for k, v in body.validated_data["pins"].items()},
            expected_version=body.validated_data.get("expected_version"),
            note=body.validated_data.get("note", ""),
        )
        return Response(ConfigPackSerializer(pack).data)

    @extend_schema(
        operation_id="packs_config_versions_run_checker",
        request=None,
        responses={201: RunDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Run the engineering checker (ACTIVE rule set) over every pack; stored as an engineering run (`engineering.verify`).",
    )
    @action(detail=True, methods=["post"], url_path="run-checker", filter_backends=[], pagination_class=None)
    def run_checker(self, request, *args, **kwargs):
        run = versions.run_checker(self.get_object(), user=request.user)
        return Response(RunDetailSerializer(run).data, status=201)

    @_transition("submit", TransitionSerializer, "DRAFT → SUBMITTED (409 `no_changes` when identical to the version it is based on).")
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def submit(self, request, *args, **kwargs):
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(versions.submit(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version")))

    @_transition("approve", ApproveSerializer, "SUBMITTED → APPROVED (`direct` approves a DRAFT); the previous approved version becomes SUPERSEDED.")
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def approve(self, request, *args, **kwargs):
        body = ApproveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        return self._detail(versions.approve(self.get_object(), user=request.user, expected_version=data.get("expected_version"), note=data.get("note", ""), direct=data.get("direct", False)))

    @_transition("reject", RejectSerializer, "SUBMITTED → REJECTED with a reason.")
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def reject(self, request, *args, **kwargs):
        body = RejectSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(versions.reject(self.get_object(), user=request.user, reason=body.validated_data["reason"], expected_version=body.validated_data.get("expected_version")))
