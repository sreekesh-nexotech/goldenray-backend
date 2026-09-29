"""``GET procurement/price-master/`` — current purchase price and landed cost per component with the batch that set them
(``procurement.view``; landed fields need ``pricing_internal.view``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view

from core.views import BaseViewSet, ListModelMixin
from pricing.views.common import InternalContextMixin
from procurement.serializers.shapes import PriceMasterEntrySerializer
from procurement.services import price_master


@extend_schema_view(
    list=extend_schema(
        operation_id="procurement_price_master_list",
        tags=["procurement"],
        description="Components with a current PURCHASE or LANDED price (a component without a committed version is absent — never a zero).",
    )
)
class PriceMasterViewSet(InternalContextMixin, ListModelMixin, BaseViewSet):
    module = "procurement"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = PriceMasterEntrySerializer
    filterset_class = price_master.PriceMasterFilter
    ordering_fields = ["sku", "name"]
    ordering = ["sku"]

    def base_queryset(self):
        return price_master.components_queryset()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        entries = price_master.entries_for(page)
        return self.get_paginated_response(self.get_serializer(entries, many=True).data)
