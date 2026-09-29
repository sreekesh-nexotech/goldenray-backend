"""Website calculators (``/api/public/v1/calculators/…``): anonymous, throttled per client IP.

``basic/`` (legacy ``calculate-solar/``), ``basic-v2/`` (legacy ``calculate-solar-new/``, what the website calls
today) and ``advanced/`` (legacy ``calculate-solar-advanced/``). They compute and write nothing, so they share the
``public_read`` budget; each answers ``Cache-Control: no-store`` and reads the cached table snapshot
(``calculators.services.data``).
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from calculators.serializers.public import (
    AdvancedRequestSerializer,
    AdvancedResponseSerializer,
    BasicRequestSerializer,
    BasicResponseSerializer,
    BasicV2ResponseSerializer,
)
from calculators.services.calculate import run
from core.serializers import ErrorSerializer
from core.views import PublicAPIView

TAGS = ["public"]
NO_STORE = {"Cache-Control": "no-store"}
ERRORS = {400: ErrorSerializer, 404: ErrorSerializer}


class _CalculatorView(PublicAPIView):
    throttle_scope = "public_read"
    kind = ""

    def post(self, request, *args, **kwargs):
        return Response(run(self.kind, request.data), headers=NO_STORE)


class BasicCalculatorView(_CalculatorView):
    kind = "basic"

    @extend_schema(
        operation_id="public_calculators_basic",
        request=BasicRequestSerializer,
        responses={200: BasicResponseSerializer, **ERRORS},
        tags=TAGS,
        auth=[],
        description="System size from the monthly bill through the KSEB slabs, priced by size (legacy `calculate-solar/`). 404 `pincode_not_found`.",
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class BasicV2CalculatorView(_CalculatorView):
    kind = "basic_v2"

    @extend_schema(
        operation_id="public_calculators_basic_v2",
        request=BasicRequestSerializer,
        responses={200: BasicV2ResponseSerializer, **ERRORS},
        tags=TAGS,
        auth=[],
        description="The bill band's system and price for the property type, a 25-year graph and a 10-year EMI (legacy `calculate-solar-new/`).",
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class AdvancedCalculatorView(_CalculatorView):
    kind = "advanced"

    @extend_schema(
        operation_id="public_calculators_advanced",
        request=AdvancedRequestSerializer,
        responses={200: AdvancedResponseSerializer, **ERRORS},
        tags=TAGS,
        auth=[],
        description="Appliance- and EV-based sizing with an optional backup battery (legacy `calculate-solar-advanced/`).",
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)
