"""Public delivery (website): ``content/<collection>/``, ``content/<collection>/<slug>/`` and ``content/preview/<token>/``.

Anonymous, throttled ``public_read``. Collection and slug responses are cached through the version-keyed response
cache, keyed by path + the query string **as sent** (``ordered_query``: sort keys apply in query-string order and a
repeated filter or page value uses its last occurrence, so reordered queries must never share an entry) and depending
on every namespace the payload embeds (collections, templates, entries, authors, categories, tags, badges, media).
Preview responses are never cached (``private, no-store``, ``noindex``).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from blog.serializers.delivery import DeliveryResponseSerializer, page_body
from blog.services import delivery, preview
from blog.services.common import DELIVERY_NAMESPACES
from core.serializers import ErrorSerializer
from core.views import PublicAPIView
from flarize.cache_utils import cache_response

TAGS = ["public"]
CACHE_TTL = 300
QUERY_PARAMETERS = [
    OpenApiParameter("populate", str, description="accepted for Strapi compatibility; responses are always fully populated"),
    OpenApiParameter(
        "filters[slug][$eq]",
        str,
        description=(
            "filters[<field>][<op>]: fields slug, title, documentId, isFeatured, sortOrder, publishedOn, publishedAt, updatedAt, readTime; "
            "ops $eq $eqi $in $contains $ne $null $gt $gte $lt $lte (unknown fields/ops are ignored; a value that does not parse is 400 invalid_filter)"
        ),
    ),
    OpenApiParameter("fields[0]", str, description="scalar field selection (id and documentId are always returned)"),
    OpenApiParameter("sort[0]", str, description="field, field:asc or field:desc (same field names as filters)"),
    OpenApiParameter("pagination[page]", int, description="default 1"),
    OpenApiParameter("pagination[pageSize]", int, description="default 25, clamped to 1…200"),
]
RESPONSES = {200: DeliveryResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer}


class CollectionDeliveryView(PublicAPIView):
    @extend_schema(operation_id="public_content_list", parameters=QUERY_PARAMETERS, responses=RESPONSES, tags=TAGS, auth=[], description="Published entries of a collection (Strapi-v5-flat).")
    @cache_response(namespaces=list(DELIVERY_NAMESPACES), ttl=CACHE_TTL, ordered_query=True)
    def get(self, request, *args, **kwargs):
        collection = delivery.active_collection(kwargs["collection"])
        page = delivery.query_entries(collection, delivery.parse_query(request.query_params))
        return Response(page_body(page))


class EntryDeliveryView(PublicAPIView):
    @extend_schema(
        operation_id="public_content_entry",
        parameters=[QUERY_PARAMETERS[0], QUERY_PARAMETERS[2]],
        responses=RESPONSES,
        tags=TAGS,
        auth=[],
        description="One published entry as a one-item list; an old slug resolves through the slug history (meta.redirect).",
    )
    @cache_response(namespaces=list(DELIVERY_NAMESPACES), ttl=CACHE_TTL, ordered_query=True)
    def get(self, request, *args, **kwargs):
        collection = delivery.active_collection(kwargs["collection"])
        page = delivery.entry_by_slug(collection, kwargs["slug"], delivery.parse_query(request.query_params))
        return Response(page_body(page))


class EntryPreviewView(PublicAPIView):
    @extend_schema(
        operation_id="public_content_preview",
        parameters=[QUERY_PARAMETERS[2]],
        responses={200: DeliveryResponseSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 410: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="The current state of an entry (any status) behind a signed preview token issued in the Studio.",
    )
    def get(self, request, *args, **kwargs):
        entry = preview.resolve_preview(kwargs["token"])
        page = delivery.preview_page(entry, delivery.parse_query(request.query_params))
        response = Response(page_body(page, extra_meta={"preview": True}))
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
