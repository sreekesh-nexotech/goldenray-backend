"""``/api/public/<version>/products/…`` — the website's product pages (PLAN §3.3; replaces the legacy
``/api/solar-panels/``, ``/api/solar-inverters/``, ``/api/batteries/``).

Anonymous, throttled ``public_read``, cached (server TTL 300 s, ``Cache-Control`` ≤ 60 s) under the ``catalog``,
``pricing`` and ``media`` namespaces, so a catalog edit, a price release or a media change is visible at once.
Lists are paginated (``page``, ``page_size`` ≤ 200) and ordered by Kerala climate score by default.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from catalog.filters import PublicBatteryFilter, PublicInverterFilter, PublicOrderingFilter, PublicPanelFilter
from catalog.serializers.profiles import PublicProductDetailSerializer, PublicProductSerializer
from catalog.services import public
from catalog.services.common import PUBLIC_CACHE_NAMESPACES
from core.serializers import ErrorSerializer
from core.views import PublicViewMixin
from flarize.cache_utils import cache_response
from flarize.filters import FilterBackend

TAGS = ["public"]
CACHE_TTL = 300


COMMON_ORDERING = {
    "kerala_climate_score": "kerala_climate_score",
    "published_at": "published_at",
    "slug": "slug",
    "product_warranty": "component__warranty_product_years",
    "performance_warranty": "component__warranty_performance_years",
    # Earlier spellings, kept accepted.
    "component__warranty_product_years": "component__warranty_product_years",
    "component__warranty_performance_years": "component__warranty_performance_years",
}


class _ProductListView(PublicViewMixin, GenericAPIView):
    category = ""
    serializer_class = PublicProductSerializer
    filter_backends = [FilterBackend, PublicOrderingFilter]
    # No default ``ordering``: without ?ordering= the service order applies (score descending, unscored last).
    ordering_aliases: dict[str, str] = COMMON_ORDERING
    ordering_fields = list(COMMON_ORDERING)

    def get_queryset(self):
        return public.products_in(self.category)

    @cache_response(namespaces=PUBLIC_CACHE_NAMESPACES, ttl=CACHE_TTL)
    def get(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = PublicProductSerializer(page, many=True, context={**self.get_serializer_context(), "prices": public.price_context(page)})
        return self.get_paginated_response(serializer.data)


def _list_schema(operation_id: str, what: str):
    return extend_schema(operation_id=operation_id, responses={200: PublicProductSerializer(many=True)}, tags=TAGS, auth=[], description=f"Published {what} (profile + spec + price range).")


class PanelListView(_ProductListView):
    category = "panel"
    filterset_class = PublicPanelFilter
    ordering_aliases = {**COMMON_ORDERING, "efficiency": "component__panel_spec__efficiency_pct", "wattage": "component__panel_spec__wattage_w"}
    ordering_fields = list(ordering_aliases)

    @_list_schema("public_products_panels_list", "solar panels")
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class InverterListView(_ProductListView):
    category = "inverter"
    filterset_class = PublicInverterFilter
    ordering_aliases = {
        **COMMON_ORDERING,
        "efficiency": "component__inverter_spec__efficiency_pct",
        "kw": "component__inverter_spec__kw",
        "extendable_warranty": "component__warranty_extendable_years",
    }
    ordering_fields = list(ordering_aliases)

    @_list_schema("public_products_inverters_list", "inverters")
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class BatteryListView(_ProductListView):
    category = "battery"
    filterset_class = PublicBatteryFilter
    ordering_aliases = {**COMMON_ORDERING, "capacity_kwh": "component__battery_spec__capacity_kwh"}
    ordering_fields = list(ordering_aliases)

    @_list_schema("public_products_batteries_list", "batteries")
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ProductDetailView(PublicViewMixin, GenericAPIView):
    serializer_class = PublicProductDetailSerializer

    @extend_schema(
        operation_id="public_products_retrieve",
        parameters=[
            OpenApiParameter("category", str, OpenApiParameter.PATH, description="Category slug (panel, inverter, battery, …) or panels / inverters / batteries."),
            OpenApiParameter("slug", str, OpenApiParameter.PATH, description="Public profile slug."),
        ],
        responses={200: PublicProductDetailSerializer, 404: ErrorSerializer},
        tags=TAGS,
        auth=[],
    )
    @cache_response(namespaces=PUBLIC_CACHE_NAMESPACES, ttl=CACHE_TTL)
    def get(self, request, category: str, slug: str, *args, **kwargs):
        profile = public.get_product(category, slug)
        return Response(PublicProductDetailSerializer(profile, context={"request": request, "prices": public.price_context([profile])}).data)
