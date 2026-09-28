"""OTP throttles (PLAN §3.1 ``otp`` 5/10min per phone; plus ``otp_ip`` per client IP).

Both apply to ``otp/send`` (and ``otp_ip`` to ``otp/verify``). The phone key is the normalised E.164 number, so
spelling variants (``+91 98765-43210`` / ``9876543210``) share one budget; a body without a valid number falls back to
the client IP. Like every throttle here they fail open on a cache outage — the database cap in
:func:`leads.services.otp.send_code` still holds.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured
from rest_framework import throttling

from customers.services.phones import try_normalise
from flarize.throttles import ScopedRateThrottle

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
        except Exception:  # noqa: BLE001 - cache outage: fail open (flarize.throttles)
            logger.warning("throttle cache unavailable; allowing request", extra={"scope": self.scope}, exc_info=True)
            return True

    def ident(self, request) -> str:
        return f"ip:{self.get_ident(request)}"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.ident(request)}


class OtpPhoneThrottle(FixedScopeThrottle):
    scope_name = "otp"

    def ident(self, request) -> str:
        data = request.data if hasattr(request.data, "get") else {}
        phone = try_normalise(data.get("phone") or "", mobile_only=True)
        return f"phone:{phone}" if phone else super().ident(request)


class OtpIpThrottle(FixedScopeThrottle):
    scope_name = "otp_ip"
