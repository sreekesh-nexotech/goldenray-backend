"""OTP throttles (PLAN §3.1 ``otp`` 5/10min per phone; plus ``otp_ip`` per client IP).

Both apply to ``otp/send`` and ``otp/verify`` (the per-phone budget of each endpoint is a bucket of its own). The phone
key is the normalised E.164 number, so spelling variants (``+91 98765-43210`` / ``9876543210``) share one budget; a body
without a valid number falls back to the client IP. Like every throttle here they fail open on a cache outage — the database cap in
:func:`leads.services.otp.send_code` still holds.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured
from rest_framework import throttling

from customers.services.phones import try_normalise
from flarize.throttles import CacheUnavailable, ScopedRateThrottle

logger = logging.getLogger("flarize.throttles")


class FixedScopeThrottle(ScopedRateThrottle):
    """A throttle with its own scope (a view may stack several), keyed by :meth:`ident`."""

    scope_name: str = ""

    def allow_request(self, request, view):
        self.scope = self.scope_name
        if self.scope not in self.THROTTLE_RATES:
            raise ImproperlyConfigured(f"No throttle rate configured for scope {self.scope!r}.")
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        try:
            return throttling.SimpleRateThrottle.allow_request(self, request, view)
        except CacheUnavailable:  # cache outage: fail open (flarize.throttles)
            logger.warning("throttle cache unavailable; allowing request", extra={"scope": self.scope}, exc_info=True)
            return True

    def ident(self, request) -> str:
        return f"ip:{self.get_ident(request)}"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.ident(request)}


class OtpPhoneThrottle(FixedScopeThrottle):
    scope_name = "otp"
    bucket = "phone"

    def ident(self, request) -> str:
        data = request.data if hasattr(request.data, "get") else {}
        phone = try_normalise(data.get("phone") or "", mobile_only=True)
        return f"{self.bucket}:{phone}" if phone else super().ident(request)


class OtpVerifyPhoneThrottle(OtpPhoneThrottle):
    """``otp`` on ``otp/verify`` (PLAN §3.3): the same per-phone rate in a bucket of its own, so a mistyped code does
    not use up the budget for sending codes (and sends do not use up the checks)."""

    bucket = "verify-phone"


class OtpIpThrottle(FixedScopeThrottle):
    scope_name = "otp_ip"
