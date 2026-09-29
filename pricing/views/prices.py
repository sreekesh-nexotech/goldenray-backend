"""``pricing/prices/`` (history + manual rows) and ``pricing/current/`` (the ``pricing_current_price`` view).

``pricing.view`` reads (PURCHASE/LANDED rows only with ``pricing_internal.view``) · ``pricing.edit`` adds a LIST or
LANDED row (LANDED also needs ``pricing_internal.view``); the previous current row is closed in the same transaction.
"""

from __future__ import annotations

import django_filters
from django.db.models import Q
from drf_spectacular.utils import extend_schema, extend_schema_view

from core.views import BaseViewSet, CreateModelMixin, ListModelMixin, RetrieveModelMixin
from pricing.models import CurrentPrice, Price, PriceKind, PriceSource
from pricing.serializers.prices import CurrentPriceSerializer, PriceCreateSerializer, PriceRowSerializer
from pricing.services import prices
from pricing.views.common import READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS, InternalContextMixin


class PriceFilter(django_filters.FilterSet):
    component = django_filters.UUIDFilter(field_name="component__uid", help_text="Component uid.")
    sku = django_filters.CharFilter(field_name="component__sku", lookup_expr="iexact")
    kind = django_filters.MultipleChoiceFilter(choices=PriceKind.choices)
    source = django_filters.MultipleChoiceFilter(choices=PriceSource.choices)
    current = django_filters.BooleanFilter(field_name="effective_to", lookup_expr="isnull", help_text="true: open rows only.")
    effective_on = django_filters.DateFilter(method="filter_effective_on", help_text="Rows in force on this date.")
    supplier = django_filters.UUIDFilter(field_name="supplier__uid")
    version_key = django_filters.CharFilter()

    class Meta:
        model = Price
        fields: list[str] = []

    def filter_effective_on(self, queryset, name, value):
        return queryset.filter(effective_from__lte=value).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=value))


class CurrentPriceFilter(django_filters.FilterSet):
    component = django_filters.UUIDFilter(field_name="component__uid")
    sku = django_filters.CharFilter(field_name="component__sku", lookup_expr="iexact")
    kind = django_filters.MultipleChoiceFilter(choices=PriceKind.choices)
    category = django_filters.CharFilter(field_name="component__category__slug")

    class Meta:
        model = CurrentPrice
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="pricing_prices_list", tags=TAGS, description="Price history (append-only rows). PURCHASE/LANDED rows need pricing_internal.view."),
    retrieve=extend_schema(operation_id="pricing_prices_retrieve", responses={200: PriceRowSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(
        operation_id="pricing_prices_create",
        request=PriceCreateSerializer,
        responses={201: PriceRowSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Adds a manual LIST or LANDED row and closes the current one. PURCHASE prices come from procurement batches.",
    ),
)
class PriceViewSet(InternalContextMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, BaseViewSet):
    module = "pricing"
    action_permissions = {"list": "view", "retrieve": "view", "create": "edit"}
    services = {"create": prices.create_manual_price}
    http_method_names = ["get", "post"]
    lookup_value_regex = UUID_REGEX
    serializer_class = PriceRowSerializer
    filterset_class = PriceFilter
    search_fields = ["component__sku", "component__name", "version_key", "source_ref"]
    ordering_fields = ["effective_from", "created_at", "amount"]
    ordering = ["component__sku", "kind", "-effective_from"]

    def base_queryset(self):
        return prices.prices_queryset(self.request.user)

    def get_serializer_class(self):
        return PriceCreateSerializer if self.action == "create" else PriceRowSerializer


@extend_schema_view(list=extend_schema(operation_id="pricing_current_list", tags=TAGS, description="The current (open) row per component and kind (Postgres view)."))
class CurrentPriceViewSet(InternalContextMixin, ListModelMixin, BaseViewSet):
    module = "pricing"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = CurrentPriceSerializer
    filterset_class = CurrentPriceFilter
    search_fields = ["component__sku", "component__name"]
    ordering_fields = ["effective_from", "amount"]
    ordering = ["component__sku", "kind"]

    def base_queryset(self):
        return prices.current_queryset(self.request.user)
