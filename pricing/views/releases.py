"""``pricing/releases/`` — list, detail by number, ``preview/`` (the publish report), publish, ``current/``, ``current/diff/``.

``pricing.view`` reads and previews · ``pricing.publish`` publishes. Landed costs and margin configuration in payloads
and diffs need ``pricing_internal.view``.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import DomainError, NotFound
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from pricing.models import PriceRelease, ReleaseStatus
from pricing.serializers.releases import PublishReportSerializer, PublishSerializer, ReleaseDetailSerializer, ReleaseDiffSerializer, ReleaseSerializer
from pricing.services import releases
from pricing.services.common import can_see_internal
from pricing.views.common import READ_ERRORS, TAGS, WRITE_ERRORS, InternalContextMixin


class ReleaseFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ReleaseStatus.choices)

    class Meta:
        model = PriceRelease
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="pricing_releases_list", tags=TAGS),
    retrieve=extend_schema(operation_id="pricing_releases_retrieve", responses={200: ReleaseDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class ReleaseViewSet(InternalContextMixin, ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "pricing"
    action_permissions = {"list": "view", "retrieve": "view", "preview": "view", "current": "view", "diff": "view", "create": "publish"}
    http_method_names = ["get", "post"]
    lookup_field = "number"
    lookup_url_kwarg = "number"
    lookup_value_regex = "[0-9]+"
    serializer_class = ReleaseSerializer
    filterset_class = ReleaseFilter
    ordering_fields = ["number", "published_at"]
    ordering = ["-number"]

    def base_queryset(self):
        if self.action == "retrieve":
            return PriceRelease.objects.select_related("market_rate_set", "published_by")
        return releases.releases_queryset()

    def get_serializer_class(self):
        return ReleaseDetailSerializer if self.action in ("retrieve", "current") else ReleaseSerializer

    @extend_schema(
        operation_id="pricing_releases_preview",
        request=None,
        responses={200: PublishReportSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Builds the next release without writing and returns the publish report (every BLOCK/WARN/INFO item).",
    )
    @action(detail=False, methods=["post"], filter_backends=[], pagination_class=None)
    def preview(self, request, *args, **kwargs):
        return Response(PublishReportSerializer(releases.preview()).data)

    @extend_schema(
        operation_id="pricing_releases_publish",
        request=PublishSerializer,
        responses={201: ReleaseDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Publishes the next PriceRelease (409 `publish_blocked` while the report has BLOCK items); the previous one becomes SUPERSEDED.",
    )
    def create(self, request, *args, **kwargs):
        body = PublishSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        release = releases.publish(user=request.user, note=body.validated_data.get("note", ""), expected_current_number=body.validated_data.get("expected_current_number"))
        return Response(ReleaseDetailSerializer(release, context=self.get_serializer_context()).data, status=201)

    @extend_schema(operation_id="pricing_releases_current", responses={200: ReleaseDetailSerializer, **READ_ERRORS}, tags=TAGS, description="The current (PUBLISHED) release.")
    @action(detail=False, methods=["get"], filter_backends=[], pagination_class=None)
    def current(self, request, *args, **kwargs):
        release = releases.current_release()
        if release is None:
            raise NotFound("no_current_release", "No PriceRelease has been published yet.")
        return Response(ReleaseDetailSerializer(release, context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="pricing_releases_current_diff",
        parameters=[OpenApiParameter("against", int, description="Release number to compare with (default: the one before the current).")],
        responses={200: ReleaseDiffSerializer, **READ_ERRORS},
        tags=TAGS,
    )
    @action(detail=False, methods=["get"], url_path="current/diff", filter_backends=[], pagination_class=None)
    def diff(self, request, *args, **kwargs):
        against = request.query_params.get("against")
        if against not in (None, "") and not str(against).isdigit():
            raise DomainError("validation_error", "against must be a release number.", errors={"against": ["A positive integer."]})
        result = releases.diff(against=int(against) if against else None, internal=can_see_internal(request.user))
        return Response(ReleaseDiffSerializer(result).data)
