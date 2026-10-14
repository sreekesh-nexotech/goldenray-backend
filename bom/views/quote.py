"""``POST bom/build/`` (staff, ``bom.view``) and ``POST /api/public/v1/bom/quote/`` (website, ``public_write``; without the
internal ``cost_breakdown`` and ``totals``, business default B-1)."""

from __future__ import annotations

from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework.response import Response

from accounts.authentication import StudioTokenAuthentication
from accounts.services.authz import can
from bom.models import Template
from bom.serializers.quote import QUOTE_STRUCTURE_TYPES, BomBillSizingRequestSerializer, BomBuildRequestSerializer, BomBuildSerializer, BomQuotePublicSerializer, BomQuoteRequestSerializer
from bom.services import build as build_service
from bom.services import quote_csv
from bom.services import website_quote
from bom.views.masters import TAGS, WRITE_ERRORS
from core.errors import DomainError
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
            "included for callers holding `pricing_internal.view`; without it the body equals the public one. "
            "`mode=dispatch` returns the warehouse picking list instead: no prices at all, on the lines or the quote."
        ),
    )
    def post(self, request, *args, **kwargs):
        result = website_quote.quote(request.data, today=timezone.localdate())
        # The picking list is a different document, so it is a different response: a
        # browser on the warehouse floor is never sent prices it must not display.
        # Checked before the cost gate, since dispatch strips strictly more.
        if str(request.data.get("mode") or "").lower() == "dispatch":
            result = website_quote.dispatch_body(result)
        elif not can(request.user, "pricing_internal", "view"):
            result = website_quote.public_body(result)
        response = Response(result)
        response["Cache-Control"] = "no-store"
        return response


class QuoteCsvView(BaseAPIView):
    """``POST bom/quote/csv/`` — the same quote as a spreadsheet.

    Server-rendered rather than built in the browser, for the same reason dispatch mode
    strips server-side: in ``dispatch`` the sheet must contain no prices, and a client
    that assembled it from a priced response would have had the prices to begin with.

    Gated on ``bom.view`` alone: the data is what ``bom/quote/`` already returns to the
    same caller, and the per-line prices are subject to the same ``pricing_internal``
    reasoning as that endpoint -- the lines themselves are not internal, the cost
    breakdown is.
    """

    authentication_classes = [StudioTokenAuthentication]
    module = "bom"
    action_permissions = {"POST": "view"}

    @extend_schema(
        operation_id="bom_quote_csv",
        request=BomQuoteRequestSerializer,
        responses={(200, "text/csv"): OpenApiResponse(response=OpenApiTypes.STR, description="The BOM as CSV."), 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        description=(
            "The BOM as a CSV attachment. `mode=quotation` (default) carries qty, unit price, amount and the line's GST rate; "
            "`mode=dispatch` is the warehouse picking list and carries no prices at all."
        ),
    )
    def post(self, request, *args, **kwargs):
        mode = "dispatch" if str(request.data.get("mode") or "").lower() == "dispatch" else "quotation"
        result = website_quote.quote(request.data, today=timezone.localdate())
        body = website_quote.dispatch_body(result) if mode == "dispatch" else result
        response = HttpResponse(quote_csv.quote_csv(body, mode=mode), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{quote_csv.filename(body, mode=mode)}"'
        response["Cache-Control"] = "no-store"
        return response


class BillSizingView(BaseAPIView):
    """``POST bom/bill-sizing/`` — what size this customer's bill suggests."""

    authentication_classes = [StudioTokenAuthentication]
    module = "bom"
    action_permissions = {"POST": "view"}

    @extend_schema(
        operation_id="bom_bill_sizing",
        request=BomBillSizingRequestSerializer,
        responses={200: dict, 400: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        description=(
            "Turns an electricity bill into a recommended system size, through the same KSEB tariff slabs and bisection the BOM "
            "tool uses (engines.energy, driven by the energy.config cost-config key). Separate from the public "
            "`calculators/basic-v2/`, which answers from a bill-range lookup table and so gives different numbers — sales staff "
            "need the figure the BOM tool gives them."
        ),
    )
    def post(self, request, *args, **kwargs):
        from decimal import Decimal

        from bom.services import bill_sizing
        from pricing.services.cost_config import current_values

        body = BomBillSizingRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        # Only sizes the chosen system type actually offers may be recommended — hybrid
        # lists `5` where on-grid lists `5sp` and `5tp`.
        template = Template.objects.filter(system_type=data["sys_type"].upper(), is_active=True).first()
        if template is None:
            raise DomainError("bom_template_missing", f"No BOM template for {data['sys_type']}.", status=400, errors={"sys_type": [data["sys_type"]]})

        result = bill_sizing.recommend(
            bill=Decimal(str(data["bill"])),
            frequency=data["frequency"],
            house_phase=data["house_phase"],
            sizes=bill_sizing.size_options(template.sizes, template.three_phase_sizes),
            config=current_values().get("energy.config"),
        )
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
