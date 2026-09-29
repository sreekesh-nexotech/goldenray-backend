"""Old website forms under ``/legacy/api/…``: ``lead-collection-home/``, ``send-otp/``, ``verify-otp/``,
``affiliate-applications/``, ``warranty-service-requests/``, ``job-applications/`` (POST only; the legacy Studio
reads of the same URLs are not shimmed — Studio moved to ``/api/v1/``). Throttled like the canonical forms
(``public_write``; the OTP endpoints ``otp`` per phone + ``otp_ip`` per client IP).
"""

from __future__ import annotations

import math

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


class _RenamedPhone:
    """The request as the canonical throttle reads it: ``phone`` = the legacy ``phone_number``; everything else (the
    client address the fallback bucket uses) is the real request's."""

    def __init__(self, request):
        self._request = request
        data = request.data if hasattr(request.data, "get") else {}
        self.data = {"phone": data.get("phone_number") or ""}

    def __getattr__(self, name):
        return getattr(self._request, name)


def _legacy_phone(throttle_class):
    """The canonical OTP throttle keyed on the legacy ``phone_number`` field (a number it cannot normalise falls back to
    the client-IP bucket, as on the canonical endpoints)."""

    class LegacyPhoneThrottle(throttle_class):
        def ident(self, request) -> str:
            return super().ident(_RenamedPhone(request))

    return LegacyPhoneThrottle


class OtpRateLimited(DomainError):
    """A throttled OTP request (per phone or per client IP); ``wait`` = seconds until the next one is allowed."""

    def __init__(self, wait: float | None):
        self.wait = None if wait is None else max(1, math.ceil(wait))
        minutes = math.ceil(self.wait / 60) if self.wait else 0
        later = f"in {minutes} minute{'' if minutes == 1 else 's'}" if minutes else "later"
        super().__init__("rate_limit_exceeded", f"Too many code requests. Please try again {later} or contact our team directly.", status=429)


class _OtpView(_FormView):
    """Every refusal for too many requests answers the legacy send-otp 429 body ``{error: "rate_limit_exceeded",
    message, days_remaining}``: the website's quote popup shows ``message`` on a 429 (without it, it printed "try
    again after undefined days"). The legacy 30-day block is replaced by the throttles and the daily cap
    (docs/decisions/leads-customers.md), so the longest wait is under a day: ``days_remaining`` is 1."""

    throttle_scope = None

    def throttled(self, request, wait):
        raise OtpRateLimited(wait)

    def legacy_error(self, exc: DomainError) -> Response:
        if exc.status != 429:
            return super().legacy_error(exc)
        response = Response({"error": "rate_limit_exceeded", "message": exc.message, "days_remaining": 1}, status=429)
        if getattr(exc, "wait", None):
            response["Retry-After"] = str(exc.wait)
        return response


class SendOtpView(_OtpView):
    throttle_classes = [_legacy_phone(OtpPhoneThrottle), OtpIpThrottle]

    def post(self, request, *args, **kwargs):
        return Response(forms.send_otp(request.data, ip=get_client_ip(request)))


class VerifyOtpView(_OtpView):
    throttle_classes = [_legacy_phone(OtpVerifyPhoneThrottle), OtpIpThrottle]

    def post(self, request, *args, **kwargs):
        return Response(forms.verify_otp(request.data, ip=get_client_ip(request)))
