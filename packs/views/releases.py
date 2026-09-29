"""``packs/releases/`` (list, ``<number>/``, ``current/``, ``<number>/packs/``, ``preview/``, publish) and ``packs/compare/``.

``packs.view`` reads, previews and compares · ``packs.publish`` publishes. Landed cost and margins need
``pricing_internal.view``.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import DomainError, NotFound
from core.views import BaseAPIView, BaseViewSet, ListModelMixin, RetrieveModelMixin
from packs.models import PackRelease, ReleasePack, ReleaseStatus, SystemType, Tier
from packs.serializers.releases import PackCompareSerializer as CompareSerializer
from packs.serializers.releases import PackPublishReportSerializer as PublishReportSerializer
from packs.serializers.releases import PackPublishSerializer as PublishSerializer
from packs.serializers.releases import PackReleaseDetailSerializer as ReleaseDetailSerializer
from packs.serializers.releases import PackReleaseSerializer as ReleaseSerializer
from packs.serializers.releases import ReleasePackSerializer
from packs.services import releases
from packs.views.versions import READ_ERRORS, TAGS, WRITE_ERRORS
from pricing.services.common import can_see_internal


class ReleaseFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ReleaseStatus.choices)

    class Meta:
        model = PackRelease
        fields: list[str] = []


class ReleasePackFilter(django_filters.FilterSet):
    system_type = django_filters.ChoiceFilter(choices=SystemType.choices)
    tier = django_filters.ChoiceFilter(choices=Tier.choices)

    class Meta:
        model = ReleasePack
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="packs_releases_list", tags=TAGS),
    retrieve=extend_schema(operation_id="packs_releases_retrieve", responses={200: ReleaseDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class ReleaseViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "packs"
    action_permissions = {"list": "view", "retrieve": "view", "current": "view", "packs": "view", "preview": "view", "create": "publish"}
    http_method_names = ["get", "post"]
    lookup_field = "number"
    lookup_url_kwarg = "number"
    lookup_value_regex = "[0-9]+"
    serializer_class = ReleaseSerializer
    filterset_class = ReleaseFilter
    search_fields: list[str] = []
    ordering_fields = ["number", "published_at"]
    ordering = ["-number"]

    def base_queryset(self):
        if self.action in ("retrieve", "packs"):
            return PackRelease.objects.select_related("config_version", "price_release", "published_by")
        return releases.releases_queryset()

    def get_serializer_class(self):
        return ReleaseDetailSerializer if self.action == "retrieve" else ReleaseSerializer

    def _internal(self) -> dict:
        return {"request": self.request, "internal": can_see_internal(self.request.user)}

    @extend_schema(operation_id="packs_releases_current", responses={200: ReleaseDetailSerializer, **READ_ERRORS}, tags=TAGS, description="The current (PUBLISHED) PackRelease.")
    @action(detail=False, methods=["get"], filter_backends=[], pagination_class=None)
    def current(self, request, *args, **kwargs):
        release = releases.current_release()
        if release is None:
            raise NotFound("no_current_release", "No PackRelease has been published yet.")
        return Response(ReleaseDetailSerializer(release, context=self._internal()).data)

    @extend_schema(
        operation_id="packs_releases_packs",
        parameters=[OpenApiParameter("system_type", str, enum=SystemType.values), OpenApiParameter("tier", str, enum=Tier.values)],
        responses={200: ReleasePackSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="The priced packs of a release with their BOMs (landed cost and margin need pricing_internal.view).",
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def packs(self, request, *args, **kwargs):
        release = self.get_object()
        queryset = ReleasePackFilter(request.query_params, queryset=releases.release_packs(release).select_related("release")).qs
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(ReleasePackSerializer(page, many=True, context=self._internal()).data)

    @extend_schema(
        operation_id="packs_releases_preview",
        request=None,
        responses={200: PublishReportSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Builds the next PackRelease without writing and returns the publish report (items + readiness matrix).",
    )
    @action(detail=False, methods=["post"], filter_backends=[], pagination_class=None)
    def preview(self, request, *args, **kwargs):
        return Response(PublishReportSerializer(releases.preview()).data)

    @extend_schema(
        operation_id="packs_releases_publish",
        request=PublishSerializer,
        responses={201: ReleaseDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Publishes the next PackRelease from the approved configuration and the current PriceRelease (409 `publish_blocked` while the report has BLOCK items).",
    )
    def create(self, request, *args, **kwargs):
        body = PublishSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        release = releases.publish(user=request.user, note=body.validated_data.get("note", ""), expected_current_number=body.validated_data.get("expected_current_number"))
        return Response(ReleaseDetailSerializer(release, context=self._internal()).data, status=201)


class CompareView(BaseAPIView):
    module = "packs"
    action_permissions = {"GET": "view"}

    @extend_schema(
        operation_id="packs_compare",
        parameters=[OpenApiParameter("a", int, required=True, description="Release number (before)."), OpenApiParameter("b", int, required=True, description="Release number (after).")],
        responses={200: CompareSerializer, 400: WRITE_ERRORS[400], **READ_ERRORS},
        tags=TAGS,
        description="Pack by pack differences between two PackReleases: packs added/removed, price and BOM line changes.",
    )
    def get(self, request, *args, **kwargs):
        values = {}
        for name in ("a", "b"):
            raw = request.query_params.get(name, "")
            if not raw.isdigit():
                raise DomainError("validation_error", f"{name} must be a release number.", errors={name: ["A positive integer."]})
            values[name] = int(raw)
        return Response(CompareSerializer(releases.compare(values["a"], values["b"])).data)
