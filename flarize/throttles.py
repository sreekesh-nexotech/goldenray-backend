"""Scoped rate throttling keyed on the trusted client IP.

Differences from DRF's ``ScopedRateThrottle``:

* anonymous callers are identified by :func:`flarize.client_ip.get_client_ip` (never by raw ``X-Forwarded-For``);
* rates accept a multi-unit duration (``5/10min``, ``10/15min``) besides DRF's ``n/s|m|h|d``;
* rates are read from settings on every request, so ``override_settings`` works in tests;
* a view may choose its scope per request (``get_throttle_scope(request)``, e.g. public read vs write) and may key a
  scope on something other than the caller (``get_throttle_ident(request)``, e.g. the phone number for OTP);
* a cache outage fails **open** (logged): nginx ``limit_req`` is the backstop and the login lockout is DB-backed, so
  a Redis failure degrades rate limiting instead of taking the API down.
"""

from __future__ import annotations

import logging
import re

from django.core.exceptions import ImproperlyConfigured
from rest_framework import throttling
from rest_framework.settings import api_settings

from flarize.client_ip import get_client_ip

logger = logging.getLogger("flarize.throttles")

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
}
_RATE_RE = re.compile(r"^\s*(?P<count>\d+)\s*/\s*(?P<multiplier>\d*)\s*(?P<unit>[a-z]+?)s?\s*$")


def parse_rate(rate: str | None) -> tuple[int | None, int | None]:
    """``"5/10min"`` → ``(5, 600)``; ``"600/min"`` → ``(600, 60)``; ``None`` → ``(None, None)`` (unthrottled)."""
    if rate is None:
        return None, None
    match = _RATE_RE.match(rate.lower())
    if not match or match.group("unit") not in _UNIT_SECONDS:
        raise ImproperlyConfigured(f"Invalid throttle rate {rate!r}; expected '<count>/<n><unit>', e.g. '5/10min'.")
    count = int(match.group("count"))
    multiplier = int(match.group("multiplier") or 1)
    if count <= 0 or multiplier <= 0:
        raise ImproperlyConfigured(f"Invalid throttle rate {rate!r}; count and duration must be positive.")
    return count, multiplier * _UNIT_SECONDS[match.group("unit")]


class ScopedRateThrottle(throttling.ScopedRateThrottle):
    """Default throttle for every DRF view; only views that declare a scope are limited."""

    @property
    def THROTTLE_RATES(self):  # noqa: N802 - DRF attribute name
        return api_settings.DEFAULT_THROTTLE_RATES

    def parse_rate(self, rate):
        return parse_rate(rate)

    def get_ident(self, request):
        return get_client_ip(request)

    def allow_request(self, request, view):
        scope_getter = getattr(view, "get_throttle_scope", None)
        self.scope = scope_getter(request) if callable(scope_getter) else getattr(view, self.scope_attr, None)
        if not self.scope:
            return True
        if self.scope not in self.THROTTLE_RATES:
            raise ImproperlyConfigured(f"No throttle rate configured for scope {self.scope!r}.")
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        try:
            return throttling.SimpleRateThrottle.allow_request(self, request, view)
        except Exception:  # noqa: BLE001 - cache outage: fail open (see module docstring)
            logger.warning("throttle cache unavailable; allowing request", extra={"scope": self.scope}, exc_info=True)
            return True

    def get_cache_key(self, request, view):
        ident_getter = getattr(view, "get_throttle_ident", None)
        ident = ident_getter(request) if callable(ident_getter) else None
        if not ident:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                ident = f"u:{user.pk}"
            else:
                ident = f"ip:{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}
