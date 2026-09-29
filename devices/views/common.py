"""Shared pieces of the devices staff views: error envelopes for the schema, one ``now`` per response, log pagination."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse

from core.serializers import ErrorSerializer
from devices.services.common import now
from flarize.pagination import CreatedAtCursorPagination

TAGS = ["devices"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, **READ_ERRORS, 409: ErrorSerializer}
DELETED = OpenApiResponse(description="Deleted.")
LOG_PARAMETERS = [
    OpenApiParameter("sync_type", OpenApiTypes.STR, enum=["INFO", "USERS", "ATTENDANCE"]),
    OpenApiParameter("status", OpenApiTypes.STR, enum=["SUCCESS", "PARTIAL", "FAILED"]),
    OpenApiParameter("cursor", OpenApiTypes.STR, description="Opaque cursor from `next`/`previous`."),
    OpenApiParameter("page_size", OpenApiTypes.INT, description="Default 50, at most 200."),
]


class SyncLogCursorPagination(CreatedAtCursorPagination):
    """Newest first, stable under concurrent inserts; the cursor is opaque."""

    ordering = ("-started_at", "-id")


class AdmsRequestCursorPagination(CreatedAtCursorPagination):
    ordering = ("-received_at", "-id")


class MomentMixin:
    """Every row of one response is judged at the same instant (health is derived from timestamps)."""

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.setdefault("at", now())
        return context


def paginate_logs(view, queryset, serializer_class):
    paginator = SyncLogCursorPagination()
    # view=None: the log ordering is the paginator's own, never the view's list ordering (OrderingFilter)
    page = paginator.paginate_queryset(queryset, view.request, view=None)
    return paginator.get_paginated_response(serializer_class(page, many=True, context=view.get_serializer_context()).data)
