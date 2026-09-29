"""``/api/public/<version>/packs/`` and ``packs/<system_type>/<tier>/<size_kw>/`` — the current PackRelease for the
website (PLAN §3.3): anonymous, throttled ``public_read``, cached under the ``packs`` namespace (bumped on publish)."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import PublicViewMixin
from flarize.cache_utils import cache_response
from flarize.filters import FilterBackend
from packs.models import Phase, ReleasePack, SystemType, Tier
from packs.serializers.public import PublicPackGroupSerializer, PublicPackSerializer
from packs.services import public
from packs.services.common import CACHE_NAMESPACE

TAGS = ["public"]
CACHE_TTL = 300


class PublicPackFilter(django_filters.FilterSet):
    system_type = django_filters.ChoiceFilter(choices=SystemType.choices)
    tier = django_filters.ChoiceFilter(choices=Tier.choices)
    phase = django_filters.ChoiceFilter(choices=Phase.choices)
    battery_config = django_filters.ChoiceFilter(choices=[("0", "0"), ("1", "1"), ("2", "2")])
    future_ready = django_filters.BooleanFilter(method="filter_future_ready")

    class Meta:
        model = ReleasePack
        fields: list[str] = []

    def filter_future_ready(self, queryset, name, value):
        return queryset.exclude(future_size_key="") if value else queryset.filter(future_size_key="")


class PublicPackListView(PublicViewMixin, GenericAPIView):
    serializer_class = PublicPackSerializer
    filter_backends = [FilterBackend]
    filterset_class = PublicPackFilter

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation: no database access
            return ReleasePack.objects.none()
        return public.public_queryset()

    @extend_schema(
        operation_id="public_packs_list",
        responses={200: PublicPackSerializer(many=True), 400: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="The packs of the current PackRelease: customer prices (incl./excl. GST) and a BOM summary.",
    )
    @cache_response(namespaces=(CACHE_NAMESPACE,), ttl=CACHE_TTL)
    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response(PublicPackSerializer(page, many=True).data)


class PublicPackDetailView(PublicViewMixin, GenericAPIView):
    serializer_class = PublicPackGroupSerializer
    pagination_class = None

    @extend_schema(
        operation_id="public_packs_retrieve",
        parameters=[
            OpenApiParameter("system_type", str, OpenApiParameter.PATH, description="ongrid | hybrid (any case)."),
            OpenApiParameter("tier", str, OpenApiParameter.PATH, description="base | value | premium (any case)."),
            OpenApiParameter("size_kw", str, OpenApiParameter.PATH, description="kW (3, 5, 10) or a size key (5sp, 5tp)."),
        ],
        responses={200: PublicPackGroupSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="Every published pack of one system type, tier and size (standard, future-ready, battery configurations).",
    )
    @cache_response(namespaces=(CACHE_NAMESPACE,), ttl=CACHE_TTL)
    def get(self, request, system_type: str, tier: str, size_kw: str, *args, **kwargs):
        system, tier_value, rows = public.packs_for(system_type, tier, size_kw)
        return Response(PublicPackGroupSerializer({"system_type": system, "tier": tier_value, "size": size_kw, "packs": rows}).data)
