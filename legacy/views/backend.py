"""Old main-backend reads and calculators under ``/legacy/api/…`` (PLAN §6.2).

Reference lists, products, metadata and installation stats are GET-only (``public_read``, cached like their canonical
endpoints); the legacy write methods on them (``POST/PUT/DELETE``, unauthenticated in the old API — a defect) are not
shimmed: 405. The calculators and the EMI computations are POSTs throttled ``public_read`` like the canonical ones
(they write nothing) and answer ``Cache-Control: no-store``.
"""

from __future__ import annotations

from rest_framework.response import Response

from core.errors import DomainError
from flarize.cache_utils import cache_response
from leads.services import installations, pincode_directory
from legacy.services import calculators, products, reference, website
from legacy.views.base import LegacyView
from seo.services.metadata import NAMESPACE as METADATA_NAMESPACE

READ_TTL = 300
NO_STORE = {"Cache-Control": "no-store"}


class _ReferenceList(LegacyView):
    key = ""

    def get(self, request, *args, **kwargs):
        return serve_reference(self, request)


def serve_reference(view, request):
    spec = reference.LISTS[view.key]
    return cache_response(namespaces=list(spec.namespaces), ttl=READ_TTL)(lambda self, req: Response(reference.legacy_list(self.key)))(view, request)


REFERENCE_VIEWS = {key: type(f"Legacy{key.title().replace('-', '')}View", (_ReferenceList,), {"key": key, "__module__": __name__}) for key in reference.LISTS}


class SolarPanelsView(LegacyView):
    @cache_response(namespaces=products.CACHE_NAMESPACES, ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        return Response(products.panels(request.query_params))


class SolarInvertersView(LegacyView):
    @cache_response(namespaces=products.CACHE_NAMESPACES, ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        return Response(products.inverters(request.query_params))


class BatteriesView(LegacyView):
    @cache_response(namespaces=products.CACHE_NAMESPACES, ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        return Response(products.battery_rows())


class MetadataView(LegacyView):
    @cache_response(namespaces=[METADATA_NAMESPACE, "media"], ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        return Response(website.metadata_list())


class InstallationStatsView(LegacyView):
    @cache_response(namespaces=[installations.CACHE_NAMESPACE, *pincode_directory.CACHE_NAMESPACES], ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        pincode = request.query_params.get("pincode")
        if not pincode:
            return Response({"error": "Pincode parameter is required"}, status=400)
        return Response(website.installation_stats(pincode))


class _Computation(LegacyView):
    throttle_scope = "public_read"


class SolarCalculatorView(_Computation):
    endpoint = ""

    def post(self, request, *args, **kwargs):
        return Response(calculators.solar(self.endpoint, request.data), headers=NO_STORE)


CALCULATOR_VIEWS = {endpoint: type(f"Legacy{endpoint.title().replace('-', '')}View", (SolarCalculatorView,), {"endpoint": endpoint, "__module__": __name__}) for endpoint in calculators.CALCULATORS}


class _EmiView(_Computation):
    def legacy_error(self, exc: DomainError) -> Response:
        return Response({"error": calculators.message(exc.message)}, status=exc.status)


class EmiCalculateView(_EmiView):
    def post(self, request, *args, **kwargs):
        return Response(calculators.emi_calculate(request.data), headers=NO_STORE)


class EmiQuotationView(_EmiView):
    def post(self, request, *args, **kwargs):
        return Response(calculators.emi_quotation(request.data), headers=NO_STORE)


class EmiConfigView(_EmiView):
    @cache_response(namespaces=lambda view, request: calculators.emi_config_namespaces(), ttl=READ_TTL)
    def get(self, request, *args, **kwargs):
        return Response(calculators.emi_config())
