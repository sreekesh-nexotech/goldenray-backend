"""Old BOM app endpoints under ``/legacy/bom/api/…``: ``calculate/`` (the website quotation page, DV-4) and
``quotation-settings/`` and ``quotation-testimonials/`` (GET only; the legacy PUT / ``manage/`` URLs were Studio's and are
not shimmed).

``calculate/`` answers the legacy body without ``cost_breakdown`` / ``totals`` (business default B-1), legacy 400s as
``{"errors": [...]}`` (``QuoteInvalid.legacy_errors``) and other refusals as ``{"error": message}``; ``public_write``,
``no-store`` like ``POST /api/public/v1/bom/quote/``.
"""

from __future__ import annotations

from rest_framework.response import Response

from bom.services.website_quote import QuoteInvalid
from company.services.profile import CACHE_NAMESPACE as COMPANY_NAMESPACE
from core.errors import DomainError
from flarize.cache_utils import cache_response
from legacy.services import website
from legacy.views.base import LegacyView
from quotations.services.common import PUBLIC_TESTIMONIALS_NAMESPACE


class BomCalculateView(LegacyView):
    def legacy_error(self, exc: DomainError) -> Response:
        if isinstance(exc, QuoteInvalid):
            return Response({"errors": exc.legacy_errors}, status=400)
        return Response({"error": exc.message}, status=exc.status)

    def post(self, request, *args, **kwargs):
        return Response(website.bom_quote(request.data), headers={"Cache-Control": "no-store"})


class QuotationSettingsView(LegacyView):
    @cache_response(namespaces=[COMPANY_NAMESPACE, "media"], ttl=300)
    def get(self, request, *args, **kwargs):
        return Response(website.quotation_settings())


class QuotationTestimonialsView(LegacyView):
    @cache_response(namespaces=[PUBLIC_TESTIMONIALS_NAMESPACE, "media"], ttl=300)
    def get(self, request, *args, **kwargs):
        return Response(website.testimonials())
