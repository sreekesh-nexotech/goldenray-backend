"""``careers/positions/`` (module ``job_positions``) and ``careers/overview/``.

``job_positions``: view (list, detail, preview, overview) · create · edit (fields, ``close/``) · publish (``publish/``,
``unpublish/``) · archive (``archive/``, ``DELETE`` of a posting nobody applied to). The list hides ARCHIVED postings
unless ``?include_archived=true`` or ``?status=ARCHIVED``.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.services.authz import can
from careers.filters import JobPositionFilter
from careers.models import JobPosition
from careers.serializers.positions import (
    CareersOverviewSerializer,
    JobPositionCreateSerializer,
    JobPositionListSerializer,
    JobPositionSerializer,
    JobPositionUpdateSerializer,
    PositionActionSerializer,
    PositionPreviewSerializer,
)
from careers.services import positions
from careers.services.public import preview
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["careers"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
_WRITE_ERRORS = {400: ErrorSerializer, **_READ_ERRORS, 409: ErrorSerializer}
TRUE = ("1", "true", "yes")
INCLUDE_ARCHIVED = OpenApiParameter("include_archived", OpenApiTypes.BOOL, description="Also list ARCHIVED postings (default: hidden).")


def _action_schema(name: str, description: str):
    return extend_schema(operation_id=f"careers_positions_{name}", request=PositionActionSerializer, responses={200: JobPositionSerializer, **_WRITE_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="careers_positions_list", parameters=[INCLUDE_ARCHIVED], tags=TAGS),
    retrieve=extend_schema(operation_id="careers_positions_retrieve", responses={200: JobPositionSerializer, **_READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="careers_positions_create", request=JobPositionCreateSerializer, responses={201: JobPositionSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="careers_positions_update", request=JobPositionUpdateSerializer, responses={200: JobPositionSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="careers_positions_delete",
        responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS},
        tags=TAGS,
        description="Only a posting nobody applied to (409 `position_has_applications`: archive it instead).",
    ),
)
class JobPositionViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "job_positions"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "preview": "view",
        "create": "create",
        "partial_update": "edit",
        "close": "edit",
        "publish": "publish",
        "unpublish": "publish",
        "archive": "archive",
        "destroy": "archive",
    }
    services = {"create": positions.create_position, "update": positions.update_position, "destroy": positions.delete_position}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = JobPositionSerializer
    filterset_class = JobPositionFilter
    search_fields = ["title", "location", "slug"]
    ordering_fields = ["sort_order", "title", "published_at", "created_at", "application_deadline"]
    ordering = ["sort_order", "-published_at", "-created_at"]

    def base_queryset(self):
        queryset = positions.positions_queryset()
        params = self.request.query_params
        if self.action == "list" and params.get("status") != JobPosition.Status.ARCHIVED and params.get("include_archived", "").lower() not in TRUE:
            queryset = queryset.exclude(status=JobPosition.Status.ARCHIVED)
        return queryset

    def get_serializer_class(self):
        return {"list": JobPositionListSerializer, "create": JobPositionCreateSerializer, "partial_update": JobPositionUpdateSerializer}.get(self.action, JobPositionSerializer)

    def _transition(self, request, name: str):
        position = self.get_object()
        body = PositionActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        updated = positions.transition(position, name, user=request.user, expected_version=body.validated_data.get("expected_version"))
        return Response(JobPositionSerializer(positions.positions_queryset().get(pk=updated.pk)).data)

    @_action_schema("publish", "DRAFT/CLOSED/ARCHIVED → PUBLISHED. 400 `position_not_ready` lists what is missing (`errors.publish_errors`).")
    @action(detail=True, methods=["post"])
    def publish(self, request, *args, **kwargs):
        return self._transition(request, "publish")

    @_action_schema("unpublish", "PUBLISHED/CLOSED → DRAFT (off the website).")
    @action(detail=True, methods=["post"])
    def unpublish(self, request, *args, **kwargs):
        return self._transition(request, "unpublish")

    @_action_schema("close", "PUBLISHED → CLOSED: stops applications; the page stays readable with `is_open: false`.")
    @action(detail=True, methods=["post"])
    def close(self, request, *args, **kwargs):
        return self._transition(request, "close")

    @_action_schema("archive", "DRAFT/PUBLISHED/CLOSED → ARCHIVED.")
    @action(detail=True, methods=["post"])
    def archive(self, request, *args, **kwargs):
        return self._transition(request, "archive")

    @extend_schema(operation_id="careers_positions_preview", responses={200: PositionPreviewSerializer, **_READ_ERRORS}, tags=TAGS, description="The posting as the public page renders it.")
    @action(detail=True, methods=["get"])
    def preview(self, request, *args, **kwargs):
        return Response(PositionPreviewSerializer(preview(self.get_object())).data)


class CareersOverviewView(BaseAPIView):
    """Careers dashboard counts; application counts only for users who may view applications."""

    module = "job_positions"
    action_permissions = {"GET": "view"}

    @extend_schema(operation_id="careers_overview", responses={200: CareersOverviewSerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        data = positions.overview(include_applications=can(request.user, "applications", "view"))
        return Response(CareersOverviewSerializer(data).data)
