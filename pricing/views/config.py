"""``pricing/cost-config/`` (+ ``history/``), ``pricing/installation-matrix/``, ``pricing/statutory-fees/``,
``pricing/validity-policy/`` — ``pricing.view`` reads, ``pricing.edit`` writes (PLAN §3.2)."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.response import Response

from core.errors import Conflict, PermissionDenied
from core.views import BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.models import CostConfig, StatutoryFee
from pricing.serializers.config import (
    CostConfigPutResultSerializer,
    CostConfigPutSerializer,
    CostConfigRowSerializer,
    CostConfigSerializer,
    InstallationMatrixSerializer,
    InstallationMatrixUpdateSerializer,
    InstallationMatrixWriteSerializer,
    StatutoryFeeSerializer,
    StatutoryFeeUpdateSerializer,
    StatutoryFeeWriteSerializer,
    ValidityPolicyPutSerializer,
    ValidityPolicySerializer,
)
from pricing.services import cost_config, masters, validity
from pricing.services.common import can_see_internal
from pricing.views.common import READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, InternalContextMixin

CRUD_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}


def _cost_config_body() -> dict:
    keys = [{"key": spec.key, "group": spec.group, "kind": spec.kind, "description": spec.description, "internal": spec.internal} for spec in cost_config.KEYS.values()]
    rows = list(cost_config.current_rows().values())
    return {"keys": keys, "current": rows}


class CostConfigView(BaseAPIView):
    module = "pricing"
    action_permissions = {"GET": "view", "PUT": "edit"}

    def _context(self):
        return {"request": self.request, "internal": can_see_internal(self.request.user)}

    @extend_schema(operation_id="pricing_cost_config_retrieve", responses={200: CostConfigSerializer, **READ_ERRORS}, tags=TAGS, description="The key registry and the current row of every key.")
    def get(self, request, *args, **kwargs):
        return Response(CostConfigSerializer(_cost_config_body(), context=self._context()).data)

    @extend_schema(
        operation_id="pricing_cost_config_update",
        request=CostConfigPutSerializer,
        responses={200: CostConfigPutResultSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Writes a new effective row for every changed key (closing the previous one). Unknown keys → 400 `unknown_config_key`.",
    )
    def put(self, request, *args, **kwargs):
        body = CostConfigPutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        entries = [dict(entry) for entry in data["entries"]]
        if not can_see_internal(request.user) and any(cost_config.KEYS[entry["key"]].internal or entry["key"] == "cost_engine.config" for entry in entries):
            raise PermissionDenied("pricing_internal_required", "Margin configuration needs the pricing_internal permission.")
        written = cost_config.put_values(user=request.user, entries=entries, effective_from=data.get("effective_from"), note=data.get("note", ""))
        result = {"written": written, "current": list(cost_config.current_rows().values())}
        return Response(CostConfigPutResultSerializer(result, context=self._context()).data)


class CostConfigHistoryFilter(django_filters.FilterSet):
    key = django_filters.ChoiceFilter(choices=[(key, key) for key in sorted(cost_config.KEYS)])

    class Meta:
        model = CostConfig
        fields: list[str] = []


@extend_schema_view(list=extend_schema(operation_id="pricing_cost_config_history", tags=TAGS, description="Every effective row (newest first per key)."))
class CostConfigHistoryViewSet(InternalContextMixin, ListModelMixin, BaseViewSet):
    module = "pricing"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = CostConfigRowSerializer
    filterset_class = CostConfigHistoryFilter
    ordering_fields = ["effective_from", "created_at"]
    ordering = ["key", "-effective_from"]

    def base_queryset(self):
        return cost_config.history_queryset()


def crud_schema(prefix: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, **READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS}, tags=TAGS),
    )


class _CrudViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "pricing"
    action_permissions = CRUD_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)


@crud_schema("pricing_installation_matrix", InstallationMatrixSerializer, InstallationMatrixWriteSerializer, InstallationMatrixUpdateSerializer)
class InstallationMatrixViewSet(_CrudViewSet):
    services = {"create": masters.create_matrix_row, "update": masters.update_matrix_row, "destroy": masters.delete_matrix_row}
    serializer_class = InstallationMatrixSerializer
    write_serializers = {"create": InstallationMatrixWriteSerializer, "partial_update": InstallationMatrixUpdateSerializer}
    ordering_fields = ["size_kw", "install_cost"]
    ordering = ["size_kw", "phase", "installation_type"]

    def base_queryset(self):
        return masters.matrix_queryset()


class StatutoryFeeFilter(django_filters.FilterSet):
    kind = django_filters.ChoiceFilter(choices=StatutoryFee._meta.get_field("kind").choices)
    current = django_filters.BooleanFilter(field_name="effective_to", lookup_expr="isnull")

    class Meta:
        model = StatutoryFee
        fields: list[str] = []


@crud_schema("pricing_statutory_fees", StatutoryFeeSerializer, StatutoryFeeWriteSerializer, StatutoryFeeUpdateSerializer)
class StatutoryFeeViewSet(_CrudViewSet):
    services = {"create": masters.create_fee, "update": masters.update_fee, "destroy": masters.delete_fee}
    serializer_class = StatutoryFeeSerializer
    write_serializers = {"create": StatutoryFeeWriteSerializer, "partial_update": StatutoryFeeUpdateSerializer}
    filterset_class = StatutoryFeeFilter
    search_fields = ["label"]
    ordering_fields = ["kind", "capacity_kw_max", "effective_from"]
    ordering = ["kind", "phase", "capacity_kw_max"]

    def base_queryset(self):
        return masters.fees_queryset()


def _validity_body() -> dict:
    policy = validity.default_policy()
    try:
        resolved = validity.resolve()
    except Conflict:
        resolved = None
    return {
        "key": validity.DEFAULT_KEY,
        "days": policy.days if policy else None,
        "grace_days": policy.grace_days if policy else None,
        "effective_from": policy.effective_from if policy else None,
        "version_label": policy.version_label if policy else "",
        "note": policy.note if policy else "",
        "version": policy.version if policy else None,
        "windows": list(validity.windows()),
        "resolved_now": resolved,
    }


class ValidityPolicyView(BaseAPIView):
    module = "pricing"
    action_permissions = {"GET": "view", "PUT": "edit"}

    @extend_schema(
        operation_id="pricing_validity_policy_retrieve",
        responses={200: ValidityPolicySerializer, **READ_ERRORS},
        tags=TAGS,
    )
    def get(self, request, *args, **kwargs):
        return Response(ValidityPolicySerializer(_validity_body()).data)

    @extend_schema(operation_id="pricing_validity_policy_update", request=ValidityPolicyPutSerializer, responses={200: ValidityPolicySerializer, **WRITE_ERRORS}, tags=TAGS)
    def put(self, request, *args, **kwargs):
        body = ValidityPolicyPutSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        validity.put_policy(user=request.user, data=data, expected_version=expected)
        return Response(ValidityPolicySerializer(_validity_body()).data)
