"""``POST bom/build/`` (staff, ``bom.view``) and ``POST /api/public/v1/bom/quote/`` (website, ``public_write``)."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from bom.serializers.quote import BomBuildRequestSerializer, BomBuildSerializer, BomQuoteRequestSerializer, BomQuoteSerializer
from bom.services import build as build_service
from bom.services import website_quote
from bom.views.masters import TAGS, WRITE_ERRORS
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, PublicAPIView


class BuildView(BaseAPIView):
    module = "bom"
    action_permissions = {"POST": "view"}

    @extend_schema(
        operation_id="bom_build",
        request=BomBuildRequestSerializer,
        responses={200: BomBuildSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A dry BOM (engines.bom_builder) for size/phase/tier/battery band/structure from the current configuration and LIST prices; nothing is written.",
    )
    def post(self, request, *args, **kwargs):
        body = BomBuildRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(build_service.build(**body.validated_data))


class QuoteView(PublicAPIView):
    @extend_schema(
        operation_id="public_bom_quote",
        request=BomQuoteRequestSerializer,
        responses={200: BomQuoteSerializer, 400: ErrorSerializer, 429: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description=(
            "The website quotation calculator (the legacy /bom/api/calculate/ contract): BOM lines, cost breakdown, totals, pricing (market rate, "
            "offers, subsidy) and meta. Invalid input → 400 `validation_error` (the legacy messages in `message`, per field in `errors`)."
        ),
    )
    def post(self, request, *args, **kwargs):
        result = website_quote.quote(request.data, today=timezone.localdate())
        response = Response(result)
        response["Cache-Control"] = "no-store"
        return response
