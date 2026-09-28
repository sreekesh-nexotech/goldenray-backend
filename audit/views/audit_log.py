"""``GET audit/`` — the audit log viewer (module ``audit``, action ``view``), newest first, cursor-paginated."""

from drf_spectacular.utils import extend_schema, extend_schema_view

from audit.filters import AuditLogFilter
from audit.serializers import AuditLogSerializer
from audit.services.queries import audit_queryset
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin
from flarize.filters import FilterBackend
from flarize.pagination import CreatedAtCursorPagination


class AuditCursorPagination(CreatedAtCursorPagination):
    """Stable under concurrent inserts; the cursor is opaque. ``page_size`` 50 (max 200)."""

    ordering = ("-at", "-id")


@extend_schema_view(list=extend_schema(operation_id="audit_list", responses={200: AuditLogSerializer(many=True), 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=["audit"]))
class AuditLogViewSet(ListModelMixin, BaseViewSet):
    module = "audit"
    action_permissions = {"list": "view"}
    serializer_class = AuditLogSerializer
    pagination_class = AuditCursorPagination
    filter_backends = [FilterBackend]
    filterset_class = AuditLogFilter
    http_method_names = ["get"]

    def base_queryset(self):
        return audit_queryset()
