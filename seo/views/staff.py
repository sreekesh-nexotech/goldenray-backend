"""Staff ``seo/metadata/``, ``seo/redirects/`` (CRUD) and ``seo/overview/`` (module ``seo``: view / edit)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.response import Response

from core.errors import DomainError
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from flarize.pagination import StandardPagination
from seo.serializers.api import (
    PageMetadataSerializer,
    PageMetadataUpdateSerializer,
    PageMetadataWriteSerializer,
    RedirectSerializer,
    RedirectUpdateSerializer,
    RedirectWriteSerializer,
    SeoOverviewPageSerializer,
)
from seo.services import metadata, overview, redirects

TAGS = ["seo"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
PERMISSIONS = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}


def _crud_schema(prefix: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, 404: ErrorSerializer}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **ERRORS}, tags=TAGS),
    )


class _SeoCrud(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "seo"
    action_permissions = PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)


@_crud_schema("seo_metadata", PageMetadataSerializer, PageMetadataWriteSerializer, PageMetadataUpdateSerializer)
class PageMetadataViewSet(_SeoCrud):
    serializer_class = PageMetadataSerializer
    write_serializers = {"create": PageMetadataWriteSerializer, "partial_update": PageMetadataUpdateSerializer}
    services = {"create": metadata.create_metadata, "update": metadata.update_metadata, "destroy": metadata.delete_metadata}
    search_fields = ["page", "title"]
    ordering_fields = ["page", "updated_at"]
    ordering = ["page"]

    def base_queryset(self):
        return metadata.metadata_queryset()


@_crud_schema("seo_redirects", RedirectSerializer, RedirectWriteSerializer, RedirectUpdateSerializer)
class RedirectViewSet(_SeoCrud):
    serializer_class = RedirectSerializer
    write_serializers = {"create": RedirectWriteSerializer, "partial_update": RedirectUpdateSerializer}
    services = {"create": redirects.create_redirect, "update": redirects.update_redirect, "destroy": redirects.delete_redirect}
    search_fields = ["from_path", "to_path", "note"]
    ordering_fields = ["from_path", "updated_at", "status_code"]
    ordering = ["from_path"]
    filterset_fields = ["status_code"]

    def base_queryset(self):
        return redirects.redirects_queryset()


class SeoOverviewView(BaseAPIView):
    """Every SEO-bearing record, worst first, paginated; ``counts`` cover every row of the ``kind`` filter."""

    module = "seo"
    action_permissions = {"GET": "view"}

    @extend_schema(
        operation_id="seo_overview",
        parameters=[
            OpenApiParameter("kind", str, description="record type (blog, page, faq, job …)"),
            OpenApiParameter("seo_status", str, enum=list(overview.STATUSES)),
            OpenApiParameter("page", int),
            OpenApiParameter("page_size", int, description="default 25, max 200"),
        ],
        responses={200: SeoOverviewPageSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer},
        tags=TAGS,
    )
    def get(self, request, *args, **kwargs):
        kind = request.query_params.get("kind") or None
        status = request.query_params.get("seo_status") or None
        if status is not None and status not in overview.STATUSES:
            raise DomainError("validation_error", "seo_status must be error, warning or ok.", errors={"seo_status": ["Must be error, warning or ok."]})
        rows = overview.combined(kind=kind, status=status)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request, view=self) if rows is not None else []
        return Response(
            {
                "results": [overview.present(row) for row in page],
                "count": paginator.page.paginator.count if rows is not None else 0,
                "next": paginator.get_next_link() if rows is not None else None,
                "previous": paginator.get_previous_link() if rows is not None else None,
                "counts": overview.counts(kind=kind),
                "kinds": overview.kinds(),
            }
        )
