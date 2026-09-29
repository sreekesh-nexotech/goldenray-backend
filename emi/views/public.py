"""Website EMI calculator (``/api/public/v1/calculators/emi*``): anonymous, throttled per client IP.

* ``GET calculators/emi/config/`` — settings, size tiles and banks; cached server-side (``emi:config`` + the price
  source's namespaces), ETag/304, ``Cache-Control: public, max-age=60``.
* ``POST calculators/emi/`` and ``POST calculators/emi/quotation/`` — computations: nothing is written, so they share
  the ``public_read`` budget (the calculator posts on every slider move); each answers ``Cache-Control: no-store``
  and reads the cached configuration snapshot.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import PublicAPIView
from emi.serializers.public import (
    EmiBreakdownSerializer,
    EmiCalculateRequestSerializer,
    EmiQuotationRequestSerializer,
    EmiQuotationResponseSerializer,
    PublicEmiConfigSerializer,
)
from emi.services import calculator
from flarize.cache_utils import cache_response

TAGS = ["public"]
CONFIG_CACHE_TTL = 300
NO_STORE = {"Cache-Control": "no-store"}


class EmiConfigView(PublicAPIView):
    @extend_schema(
        operation_id="public_calculators_emi_config",
        responses={200: PublicEmiConfigSerializer, 503: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="The EMI calculator's settings, size tiles (with their prices) and the bank comparison table.",
    )
    @cache_response(namespaces=lambda view, request: calculator.cache_namespaces(), ttl=CONFIG_CACHE_TTL)
    def get(self, request, *args, **kwargs):
        return Response(PublicEmiConfigSerializer(calculator.public_config()).data)


class EmiCalculateView(PublicAPIView):
    throttle_scope = "public_read"

    @extend_schema(
        operation_id="public_calculators_emi_calculate",
        request=EmiCalculateRequestSerializer,
        responses={200: EmiBreakdownSerializer, 400: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="The EMI breakdown for a size tile (`size_uid`) or capacity and the customer's adjustments (legacy `emi-calculator/`).",
    )
    def post(self, request, *args, **kwargs):
        return Response(calculator.calculate(request.data), headers=NO_STORE)


class EmiQuotationView(PublicAPIView):
    throttle_scope = "public_read"

    @extend_schema(
        operation_id="public_calculators_emi_quotation",
        request=EmiQuotationRequestSerializer,
        responses={200: EmiQuotationResponseSerializer, 400: ErrorSerializer, 503: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="The calculator's down payment, rate and EMI for each of a quotation's package prices (legacy `emi-calculator/quotation/`).",
    )
    def post(self, request, *args, **kwargs):
        return Response(calculator.quotation(request.data), headers=NO_STORE)
