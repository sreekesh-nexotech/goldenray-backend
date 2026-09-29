"""Website reference endpoints (``/api/public/v1/reference/…``): anonymous, ``public_read``, cached for a day.

Lists are paginated with ``StandardPagination`` (``{results, count, next, previous}``; ``page_size`` default 25,
max 200). Every current list is shorter than 200 rows, so the website asks for ``?page_size=200`` and reads one page
(docs/decisions/careers-reference.md). Only active rows are served, in ``sort_order``. Pincodes are never listed:
``reference/pincodes/<pincode>/`` looks one up (404 when unknown or inactive).

The server-side cache lives ``REFERENCE_CACHE_TTL`` seconds and is invalidated by every staff write
(``reference:<list>`` namespace); browsers and CDNs get the platform's 60-second ``max-age``.
"""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import ListModelMixin, PublicAPIView, PublicViewMixin
from flarize.cache_utils import CachedResponseMixin, cache_response
from flarize.pagination import StandardPagination
from reference.serializers.lists import SERIALIZERS
from reference.serializers.pincodes import PublicPincodeSerializer
from reference.services import lists, lookups
from reference.services.pincodes import CACHE_NAMESPACE as PINCODES_NAMESPACE

TAGS = ["public"]
REFERENCE_CACHE_TTL = 24 * 60 * 60


class _PublicReferenceList(CachedResponseMixin, ListModelMixin, PublicViewMixin, GenericAPIView):
    spec: lists.ListSpec
    pagination_class = StandardPagination
    filter_backends: list = []
    cache_ttl = REFERENCE_CACHE_TTL

    def get_cache_namespaces(self, request):
        return [self.spec.namespace]

    def get_queryset(self):
        return lists.queryset(self.spec).filter(is_active=True)

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class _PublicTariffList(_PublicReferenceList):
    def get_cache_namespaces(self, request):
        # The schedule in force changes at midnight (``effective_from``) without any write that would bump the list,
        # so the day is part of the cache key.
        return [self.spec.namespace, f"{self.spec.namespace}@{timezone.localdate().isoformat()}"]

    def get_queryset(self):
        return lookups.current_tariffs()


def public_list_view(spec: lists.ListSpec):
    name = spec.key.replace("-", "_")
    tariffs = spec is lists.TARIFFS
    base = _PublicTariffList if tariffs else _PublicReferenceList
    description = (
        "The KSEB slab schedule in force today (per phase group; `phase` null = every phase). Paginated: pass `page_size=200` to get the whole list."
        if tariffs
        else f"Active {spec.key.replace('-', ' ')} in display order. Paginated: pass `page_size=200` to get the whole list."
    )

    @extend_schema(operation_id=f"public_reference_{name}_list", tags=TAGS, auth=[], description=description)
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    return type(f"Public{spec.model.__name__}ListView", (base,), {"spec": spec, "serializer_class": SERIALIZERS[spec.key][1], "get": get, "__module__": __name__})


PUBLIC_LIST_VIEWS = {key: public_list_view(spec) for key, spec in lists.SPECS.items()}


class PublicPincodeView(PublicAPIView):
    @extend_schema(
        operation_id="public_reference_pincode_retrieve",
        responses={200: PublicPincodeSerializer, 404: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="Look up one pincode (district, state, serviceability, post offices). Pincodes are never listed.",
    )
    @cache_response(namespaces=[PINCODES_NAMESPACE], ttl=REFERENCE_CACHE_TTL)
    def get(self, request, pincode: str, *args, **kwargs):
        found = lookups.find_pincode(pincode)
        if found is None:
            raise NotFound("not_found", "Unknown pincode.")
        return Response(PublicPincodeSerializer(found).data)
