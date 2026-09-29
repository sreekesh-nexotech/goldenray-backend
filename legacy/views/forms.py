"""Old website forms under ``/legacy/api/…``: ``lead-collection-home/``, ``send-otp/``, ``verify-otp/``,
``affiliate-applications/``, ``warranty-service-requests/``, ``job-applications/`` (POST only; the legacy Studio
reads of the same URLs are not shimmed — Studio moved to ``/api/v1/``). Throttled like the canonical forms
(``public_write``; the OTP endpoints ``otp`` per phone + ``otp_ip`` per client IP).
"""

from __future__ import annotations

from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from core.errors import DomainError
from flarize.client_ip import get_client_ip
from flarize.parsers import JSONParser
from leads.views.throttles import OtpIpThrottle, OtpPhoneThrottle, OtpVerifyPhoneThrottle
from legacy.services import forms
from legacy.views.base import LegacyView


class _FormView(LegacyView):
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    throttle_scope = "public_write"

    def legacy_error(self, exc: DomainError) -> Response:
        if isinstance(exc, forms.LegacyValidationFailed):
            return Response({"message": "Validation failed", "status": "error", "errors": exc.errors}, status=400)
        if isinstance(exc, forms.LegacyPlainErrors):
            return Response(exc.errors, status=400)
        return Response({"error": exc.message}, status=exc.status)


class LeadCollectionHomeView(_FormView):
    def post(self, request, *args, **kwargs):
        return Response(forms.submit_lead(request.data, ip=get_client_ip(request)), status=201)


class AffiliateApplicationView(_FormView):
    def post(self, request, *args, **kwargs):
        return Response(forms.submit_affiliate(request.data), status=201)


class WarrantyServiceRequestView(_FormView):
    def post(self, request, *args, **kwargs):
        return Response(forms.submit_warranty(request.data), status=201)


class JobApplicationView(_FormView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        return Response(forms.submit_application(request.data, ip=get_client_ip(request)), status=201)


def _legacy_phone(throttle_class):
    """The canonical OTP throttle keyed on the legacy ``phone_number`` field."""

    class LegacyPhoneThrottle(throttle_class):
        def ident(self, request) -> str:
            data = request.data if hasattr(request.data, "get") else {}
            proxy = type("Body", (), {"data": {"phone": data.get("phone_number") or ""}})()
            return super().ident(proxy) if data.get("phone_number") else f"ip:{self.get_ident(request)}"

    return LegacyPhoneThrottle


class SendOtpView(_FormView):
    throttle_scope = None
    throttle_classes = [_legacy_phone(OtpPhoneThrottle), OtpIpThrottle]

    def post(self, request, *args, **kwargs):
        return Response(forms.send_otp(request.data, ip=get_client_ip(request)))


class VerifyOtpView(_FormView):
    throttle_scope = None
    throttle_classes = [_legacy_phone(OtpVerifyPhoneThrottle), OtpIpThrottle]

    def post(self, request, *args, **kwargs):
        return Response(forms.verify_otp(request.data, ip=get_client_ip(request)))
