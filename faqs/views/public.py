"""Public FAQ list (website): ``GET faqs/?route=<route>[&section=][&category=<slug>]``.

``page=<route>`` is accepted as an alias of ``route`` (the legacy ``/api/faqs?page=`` form and PLAN §3.3). Anonymous,
throttled ``public_read``, version-keyed cache (namespaces ``faqs``, ``sitepages``), ``ETag``/``304``,
``Cache-Control: public, max-age=60``. Payload = the legacy ``/api/faqs`` contract with ``id`` = the FAQ uid.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from core.errors import DomainError
from core.serializers import ErrorSerializer
from core.views import PublicAPIView
from faqs.serializers import PublicFaqListSerializer
from faqs.services import delivery
from flarize.cache_utils import cache_response

CACHE_TTL = 300


class PublicFaqListView(PublicAPIView):
    @extend_schema(
        operation_id="public_faqs_list",
        parameters=[
            OpenApiParameter("route", str, description="Site path of the page, e.g. `/subsidy` (required unless `page` is given)."),
            OpenApiParameter("page", str, description="Legacy alias of `route`."),
            OpenApiParameter("section", str, description="Exact section; present-but-empty selects the unnamed section."),
            OpenApiParameter("category", str, description="Category slug."),
        ],
        responses={200: PublicFaqListSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 429: ErrorSerializer},
        tags=["public"],
        auth=[],
    )
    @cache_response(namespaces=delivery.PUBLIC_CACHE_NAMESPACES, ttl=CACHE_TTL)
    def get(self, request, *args, **kwargs):
        params = request.query_params
        route = params.get("route") or params.get("page") or ""
        if not route:
            raise DomainError("validation_error", "A 'route' query parameter is required.", errors={"route": ["This field is required."]})
        section = params.get("section") if "section" in params else None
        return Response(delivery.faq_list(route, section=section, category=params.get("category") or None))
