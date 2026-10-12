"""``POST bom/build/`` (staff, ``bom.view``) and ``POST /api/public/v1/bom/quote/`` (website, ``public_write``; without the
internal ``cost_breakdown`` and ``totals``, business default B-1)."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from accounts.authentication import StudioTokenAuthentication
from accounts.services.authz import can
from bom.models import Template
from bom.serializers.quote import QUOTE_STRUCTURE_TYPES, BomBuildRequestSerializer, BomBuildSerializer, BomQuotePublicSerializer, BomQuoteRequestSerializer
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


class StaffQuoteView(BaseAPIView):
    authentication_classes = [StudioTokenAuthentication]  # platform token, or a Content Studio token (single sign-in)
    module = "bom"
    action_permissions = {"POST": "view"}

    @extend_schema(
        operation_id="bom_quote",
        request=BomQuoteRequestSerializer,
        responses={200: dict, 400: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        description=(
            "The internal BOM calculator: the same engine and request as the public quote, with the `cost_breakdown` and `totals` (cost and margin) "
            "included for callers holding `pricing_internal.view`; without it the body equals the public one."
        ),
    )
    def post(self, request, *args, **kwargs):
        result = website_quote.quote(request.data, today=timezone.localdate())
        if not can(request.user, "pricing_internal", "view"):
            result = website_quote.public_body(result)
        response = Response(result)
        response["Cache-Control"] = "no-store"
        return response


class QuoteOptionsView(BaseAPIView):
    authentication_classes = [StudioTokenAuthentication]
    module = "bom"
    action_permissions = {"GET": "view"}

    @extend_schema(
        operation_id="bom_quote_options",
        responses={200: dict},
        tags=TAGS,
        description="What the quote accepts per system type (active templates): sizes, three-phase sizes, tiers, hybrid battery bands; plus structure and subsidy types.",
    )
    def get(self, request, *args, **kwargs):
        templates = Template.objects.filter(is_active=True).order_by("system_type", "id")
        return Response(
            {
                "system_types": {
                    t.system_type.lower(): {
                        "name": t.name,
                        "sizes": t.sizes,
                        "three_phase_sizes": t.three_phase_sizes,
                        "tiers": t.tiers,
                        "battery_configs": t.battery_configs,
                    }
                    for t in templates
                },
                "structure_types": list(QUOTE_STRUCTURE_TYPES),
                "subsidy_types": ["none", "residential", "ghs"],
            }
        )


class QuoteView(PublicAPIView):
    @extend_schema(
        operation_id="public_bom_quote",
        request=BomQuoteRequestSerializer,
        responses={200: BomQuotePublicSerializer, 400: ErrorSerializer, 429: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description=(
            "The website quotation calculator (the legacy /bom/api/calculate/ contract): BOM lines, pricing (market rate, offers, subsidy), meta "
            "and available offers. The internal cost breakdown and totals (cost and margin) are never public (business default B-1). "
            "Invalid input → 400 `validation_error` (the legacy messages in `message`, per field in `errors`)."
        ),
    )
    def post(self, request, *args, **kwargs):
        result = website_quote.quote(request.data, today=timezone.localdate())
        response = Response(website_quote.public_body(result))
        response["Cache-Control"] = "no-store"
        return response
