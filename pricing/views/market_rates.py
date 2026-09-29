"""``pricing/market-rate-sets/`` — CRUD, ``rates/``, ``swap-deltas/``, ``roof-addons/`` (GET + bulk PUT), ``activate/``.

``market_rates.view`` reads · ``market_rates.edit`` creates/edits DRAFT sets and their children · ``market_rates.publish``
activates (exactly one ACTIVE set).
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.models import MarketRateSet, MarketRateSetStatus
from pricing.serializers.market_rates import (
    ActivateSerializer,
    MarketRateSerializer,
    MarketRateSetSerializer,
    MarketRateSetUpdateSerializer,
    MarketRateSetWriteSerializer,
    MarketRatesPutSerializer,
    ReplaceResultSerializer,
    RoofAddonSerializer,
    RoofAddonsPutSerializer,
    SwapDeltaSerializer,
    SwapDeltasPutSerializer,
)
from pricing.services import market_rates
from pricing.views.common import READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, PatchOnlyUpdateMixin


class MarketRateSetFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=MarketRateSetStatus.choices)

    class Meta:
        model = MarketRateSet
        fields: list[str] = []


def _children(name: str, read, put, description: str):
    return {
        "get": extend_schema(operation_id=f"pricing_market_rate_sets_{name}_list", responses={200: read(many=True), **READ_ERRORS}, tags=TAGS),
        "put": extend_schema(operation_id=f"pricing_market_rate_sets_{name}_replace", request=put, responses={200: ReplaceResultSerializer, **WRITE_ERRORS}, tags=TAGS, description=description),
    }


RATES = _children(
    "rates", MarketRateSerializer, MarketRatesPutSerializer, "Replaces the set's rates (upsert by system/tier/battery/size/from/future/variant; left-out rows are removed). DRAFT sets only."
)
DELTAS = _children("swap_deltas", SwapDeltaSerializer, SwapDeltasPutSerializer, "Replaces the set's swap deltas (DRAFT sets only).")
ADDONS = _children("roof_addons", RoofAddonSerializer, RoofAddonsPutSerializer, "Replaces the set's roof add-ons (DRAFT sets only).")


@extend_schema_view(
    list=extend_schema(operation_id="pricing_market_rate_sets_list", tags=TAGS),
    retrieve=extend_schema(operation_id="pricing_market_rate_sets_retrieve", responses={200: MarketRateSetSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="pricing_market_rate_sets_create", request=MarketRateSetWriteSerializer, responses={201: MarketRateSetSerializer, **WRITE_ERRORS}, tags=TAGS),
    update=extend_schema(exclude=True),
    partial_update=extend_schema(operation_id="pricing_market_rate_sets_update", request=MarketRateSetUpdateSerializer, responses={200: MarketRateSetSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="pricing_market_rate_sets_delete",
        responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS},
        tags=TAGS,
        description="Refused for the ACTIVE set and for sets a release was built from.",
    ),
)
class MarketRateSetViewSet(PatchOnlyUpdateMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "market_rates"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "rates": "view",
        "swap_deltas": "view",
        "roof_addons": "view",
        "create": "edit",
        "update": "edit",
        "partial_update": "edit",
        "destroy": "edit",
        "replace_rates": "edit",
        "replace_swap_deltas": "edit",
        "replace_roof_addons": "edit",
        "activate": "publish",
    }
    services = {"create": market_rates.create_set, "update": market_rates.update_set, "destroy": market_rates.delete_set}
    http_method_names = ["get", "post", "put", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = MarketRateSetSerializer
    filterset_class = MarketRateSetFilter
    search_fields = ["name"]
    ordering_fields = ["created_at", "name", "activated_at"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return market_rates.sets_queryset()

    def get_serializer_class(self):
        return {"create": MarketRateSetWriteSerializer, "partial_update": MarketRateSetUpdateSerializer}.get(self.action, MarketRateSetSerializer)

    def _page(self, queryset, serializer_class):
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(serializer_class(page, many=True, context=self.get_serializer_context()).data)

    def _replaced(self, rate_set, outcome):
        refreshed = market_rates.sets_queryset().get(pk=rate_set.pk)
        return Response(ReplaceResultSerializer({"set": refreshed, "outcome": outcome}).data)

    def _body(self, serializer_class):
        body = serializer_class(data=self.request.data)
        body.is_valid(raise_exception=True)
        return body.validated_data

    @RATES["get"]
    @action(detail=True, methods=["get"], filter_backends=[])
    def rates(self, request, *args, **kwargs):
        return self._page(market_rates.rates_of(self.get_object()), MarketRateSerializer)

    @RATES["put"]
    @rates.mapping.put
    def replace_rates(self, request, *args, **kwargs):
        data = self._body(MarketRatesPutSerializer)
        rate_set = self.get_object()
        outcome = market_rates.put_rates(rate_set, user=request.user, rows=[dict(row) for row in data["rates"]], expected_version=data.get("expected_version"))
        return self._replaced(rate_set, outcome)

    @DELTAS["get"]
    @action(detail=True, methods=["get"], url_path="swap-deltas", filter_backends=[])
    def swap_deltas(self, request, *args, **kwargs):
        return self._page(market_rates.swap_deltas_of(self.get_object()), SwapDeltaSerializer)

    @DELTAS["put"]
    @swap_deltas.mapping.put
    def replace_swap_deltas(self, request, *args, **kwargs):
        data = self._body(SwapDeltasPutSerializer)
        rate_set = self.get_object()
        outcome = market_rates.put_swap_deltas(rate_set, user=request.user, rows=[dict(row) for row in data["swap_deltas"]], expected_version=data.get("expected_version"))
        return self._replaced(rate_set, outcome)

    @ADDONS["get"]
    @action(detail=True, methods=["get"], url_path="roof-addons", filter_backends=[])
    def roof_addons(self, request, *args, **kwargs):
        return self._page(market_rates.roof_addons_of(self.get_object()), RoofAddonSerializer)

    @ADDONS["put"]
    @roof_addons.mapping.put
    def replace_roof_addons(self, request, *args, **kwargs):
        data = self._body(RoofAddonsPutSerializer)
        rate_set = self.get_object()
        outcome = market_rates.put_roof_addons(rate_set, user=request.user, rows=[dict(row) for row in data["roof_addons"]], expected_version=data.get("expected_version"))
        return self._replaced(rate_set, outcome)

    @extend_schema(
        operation_id="pricing_market_rate_sets_activate",
        request=ActivateSerializer,
        responses={200: MarketRateSetSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="DRAFT (or RETIRED) → ACTIVE; the previous ACTIVE set becomes RETIRED in the same transaction.",
    )
    @action(detail=True, methods=["post"])
    def activate(self, request, *args, **kwargs):
        data = self._body(ActivateSerializer)
        rate_set = market_rates.activate_set(self.get_object(), user=request.user, expected_version=data.get("expected_version"), note=data.get("note", ""))
        return Response(MarketRateSetSerializer(market_rates.sets_queryset().get(pk=rate_set.pk)).data)
