"""Public page payload (website): ``GET pages/<slug>/`` and the query form ``GET pages/?route=<route>``.

Anonymous, throttled ``public_read``, served through the version-keyed cache (namespaces ``sitepages``, ``media``,
``company``: a slot/SEO edit, a media alt-text edit or a company rename invalidates it), ``ETag``/``304``,
``Cache-Control: public, max-age=60``. Payload = the legacy ``/api/page-content?route=`` contract.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from core.errors import DomainError
from core.serializers import ErrorSerializer
from core.views import PublicAPIView
from flarize.cache_utils import cache_response
from sitepages.serializers import PublicPageContentSerializer
from sitepages.services import delivery

TAGS = ["public"]
CACHE_TTL = 300


class PublicPageView(PublicAPIView):
    @extend_schema(operation_id="public_pages_retrieve", responses={200: PublicPageContentSerializer, 404: ErrorSerializer, 429: ErrorSerializer}, tags=TAGS, auth=[])
    @cache_response(namespaces=delivery.PUBLIC_CACHE_NAMESPACES, ttl=CACHE_TTL)
    def get(self, request, *args, slug=None, **kwargs):
        return Response(delivery.page_content(delivery.published_page(slug=slug)))


class PublicPageByRouteView(PublicAPIView):
    @extend_schema(
        operation_id="public_pages_by_route",
        parameters=[OpenApiParameter("route", str, required=True, description="Site path of the page, e.g. `/career` (the website's query form).")],
        responses={200: PublicPageContentSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        auth=[],
    )
    @cache_response(namespaces=delivery.PUBLIC_CACHE_NAMESPACES, ttl=CACHE_TTL)
    def get(self, request, *args, **kwargs):
        route = request.query_params.get("route", "")
        if not route:
            raise DomainError("validation_error", "A 'route' query parameter is required.", errors={"route": ["This field is required."]})
        return Response(delivery.page_content(delivery.published_page(route=route)))
