"""Public ``seo/metadata/<page>/``, ``seo/redirects/`` and ``sitemap/entries/`` (anonymous, cached, ``public_read``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import ListModelMixin, PublicAPIView, PublicGenericViewSet
from flarize.cache_utils import CachedResponseMixin, cache_response
from seo.serializers.api import PublicPageMetadataSerializer, PublicRedirectSerializer, SitemapEntrySerializer
from seo.services import metadata, redirects, sitemap_feed

TAGS = ["public"]


class PublicPageMetadataView(PublicAPIView):
    @extend_schema(
        operation_id="public_seo_metadata",
        responses={200: PublicPageMetadataSerializer, 404: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="Metadata of one page (route key: 'home', 'about', 'projects/123'); replaces /api/metadata/.",
    )
    @cache_response(namespaces=[metadata.NAMESPACE, "media"], ttl=300)
    def get(self, request, *args, **kwargs):
        return Response(PublicPageMetadataSerializer(metadata.public_metadata(kwargs["page"])).data)


@extend_schema_view(list=extend_schema(operation_id="public_seo_redirects", tags=TAGS, auth=[], description="Every live redirect, for the Next.js build (paginated, up to 200 per page)."))
class PublicRedirectViewSet(CachedResponseMixin, ListModelMixin, PublicGenericViewSet):
    serializer_class = PublicRedirectSerializer
    filter_backends: list = []
    cache_namespaces = (redirects.NAMESPACE,)
    cache_ttl = 300

    def get_queryset(self):
        return redirects.redirects_queryset()


@extend_schema_view(list=extend_schema(operation_id="public_sitemap_entries", tags=TAGS, auth=[], description="Site paths + lastmod from every app's sitemap provider, sorted by path (paginated)."))
class SitemapEntriesViewSet(CachedResponseMixin, ListModelMixin, PublicGenericViewSet):
    serializer_class = SitemapEntrySerializer
    filter_backends: list = []
    cache_ttl = 300

    def get_cache_namespaces(self, request):
        return sitemap_feed.cache_namespaces()

    def get_queryset(self):
        return sitemap_feed.entries()
