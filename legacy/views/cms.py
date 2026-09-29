"""Old CMS delivery under ``/legacy/studio-api/api/…`` (nginx sends ``/studio-api/api/<x>`` here in ``shim`` mode).

Routes as the CMS had them: ``faqs``, ``job-positions``, ``job-positions/<slug>`` and ``page-content`` without a
trailing slash; ``<collection>`` with or without one (so ``faqs/`` is "Unknown collection 'faqs'", as it was).
GET only, ``public_read``, cached like the canonical delivery endpoints; 404s are the CMS ``{"detail": …}`` bodies.
"""

from __future__ import annotations

from rest_framework.response import Response

from core.errors import DomainError
from flarize.cache_utils import cache_response
from legacy.services import cms
from legacy.views.base import LegacyView

TTL = 300


class _CmsView(LegacyView):
    def legacy_error(self, exc: DomainError) -> Response:
        if isinstance(exc, cms.LegacyNotFound):
            return Response({"detail": exc.message}, status=404)
        return Response({"detail": exc.message}, status=exc.status)


class CollectionView(_CmsView):
    @cache_response(namespaces=cms.COLLECTION_NAMESPACES, ttl=TTL, ordered_query=True)
    def get(self, request, *args, api_uid: str, **kwargs):
        return Response(cms.collection(api_uid, request.query_params))


class FaqsView(_CmsView):
    @cache_response(namespaces=cms.FAQ_NAMESPACES, ttl=TTL)
    def get(self, request, *args, **kwargs):
        return Response(cms.faqs(request.query_params))


class JobPositionsView(_CmsView):
    @cache_response(namespaces=cms.CAREERS_NAMESPACES, ttl=TTL)
    def get(self, request, *args, **kwargs):
        return Response(cms.job_positions(request.query_params))


class JobPositionView(_CmsView):
    @cache_response(namespaces=cms.CAREERS_NAMESPACES, ttl=TTL)
    def get(self, request, *args, slug: str, **kwargs):
        return Response(cms.job_position(slug))


class PageContentView(_CmsView):
    @cache_response(namespaces=cms.PAGE_NAMESPACES, ttl=TTL)
    def get(self, request, *args, **kwargs):
        return Response(cms.page_content(request.query_params))
