"""``POST catalog/components/import/`` (CSV; dry run first, then commit) and ``GET catalog/components/export/``.

Import needs ``catalog.create`` (and ``catalog.edit`` for rows that update existing SKUs); export needs
``catalog.view`` and takes the component list filters. The format is described in ``catalog.services.csv_io``.
"""

from __future__ import annotations

import json

from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response

from catalog.filters import ComponentFilter
from catalog.serializers.io import ImportReportSerializer, ImportRequestSerializer
from catalog.services import components, csv_io
from core.errors import DomainError
from core.serializers import ErrorSerializer
from core.views import BaseAPIView
from flarize.filters import FilterBackend

TAGS = ["catalog"]


class CSVRenderer(BaseRenderer):
    """Lets clients ask for ``Accept: text/csv``; errors (dicts) are still rendered as JSON text."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, (dict, list)):
            return json.dumps(data).encode()
        return (data or "").encode() if isinstance(data, str) else data


class ComponentImportView(BaseAPIView):
    module = "catalog"
    action_permissions = {"POST": "create"}
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="catalog_components_import",
        request={"multipart/form-data": ImportRequestSerializer},
        responses={200: ImportReportSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 413: ErrorSerializer},
        tags=TAGS,
        description=(
            "CSV import. `dry_run=true` (default) validates and simulates every row and returns a report plus an `import_token` when it is clean; "
            "`dry_run=false` with that token commits the same file (all rows or none). 400 `dry_run_required` without a matching token, "
            "400 `import_has_errors` when a row fails at commit."
        ),
    )
    def post(self, request, *args, **kwargs):
        body = ImportRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        upload = body.validated_data["file"]
        if upload.size > csv_io.MAX_BYTES:
            raise DomainError("file_too_large", "The CSV is larger than 2 MB.", status=413)
        report = csv_io.import_csv(upload.read(), user=request.user, dry_run=body.validated_data["dry_run"], import_token=body.validated_data.get("import_token"))
        return Response(ImportReportSerializer(report).data)


class ComponentExportView(BaseAPIView):
    module = "catalog"
    action_permissions = {"GET": "view"}
    renderer_classes = [JSONRenderer, CSVRenderer]
    filterset_class = ComponentFilter

    @extend_schema(
        operation_id="catalog_components_export",
        parameters=[
            OpenApiParameter("category", str, description="Category slug(s), comma-separated."),
            OpenApiParameter("brand", str, description="Brand slug(s), comma-separated."),
            OpenApiParameter("status", str, many=True, enum=["DRAFT", "ACTIVE", "DEPRECATED", "RETIRED"]),
            OpenApiParameter("tier", str, enum=["BASE", "VALUE", "PREMIUM"]),
            OpenApiParameter("is_public", bool),
            OpenApiParameter("search", str, description="Full-text over sku, name, model, brand, description."),
        ],
        responses={(200, "text/csv"): OpenApiResponse(response=OpenApiTypes.STR, description="CSV in the import format."), 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer},
        tags=TAGS,
        description="The filtered components as CSV (the import format; at most 10,000 rows). Accepts the component list filters.",
    )
    def get(self, request, *args, **kwargs):
        queryset = self.scope_queryset(components.components_queryset())
        queryset = FilterBackend().filter_queryset(request, queryset, self)
        content = csv_io.export_csv(csv_io.exported_components(queryset))
        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="components-{timezone.localdate():%Y%m%d}.csv"'
        response["Cache-Control"] = "private, no-store"
        return response
