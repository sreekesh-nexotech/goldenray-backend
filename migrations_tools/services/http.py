"""HTTP access for the verification: the legacy APIs over the network, the new public API in process or over HTTP.

``get`` returns ``(status, parsed JSON or None)``. Query strings keep the parameter order given (the legacy CMS is
order-sensitive for repeated keys). The in-process client disables throttling for the duration of the check (the
verification replays hundreds of requests from one address), everything else runs through the real middleware,
views and cache.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlencode

from django.conf import settings

TIMEOUT_SECONDS = 20
QUIET_LOGGERS = ("flarize.request", "flarize.emi", "flarize.calculators")


def query_string(params) -> str:
    items = list(params.items()) if isinstance(params, dict) else list(params or [])
    return urlencode(items)


def _parse(body: bytes):
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


class RemoteClient:
    """GET / POST / HEAD against ``base_url`` (``http://127.0.0.1:18009``)."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _open(self, request):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - operator-given URL
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self, path: str, params=None):
        url = f"{self.base_url}{path}" + (f"?{query_string(params)}" if params else "")
        status, body = self._open(urllib.request.Request(url, headers={"Accept": "application/json"}))
        return status, _parse(body)

    def post(self, path: str, body: bytes):
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
        status, raw = self._open(request)
        return status, _parse(raw)


def head_status(url: str) -> int:
    """HTTP status of a HEAD request (redirects followed); 0 when the host cannot be reached."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - stored CDN URL
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _host() -> str:
    for host in settings.ALLOWED_HOSTS:
        host = host.lstrip(".")
        if host and host != "*" and not host.startswith("["):
            return host
    return "localhost"


@contextmanager
def unthrottled():
    """No throttling, and no per-request INFO access-log lines, while the verification replays its requests."""
    from django.test.utils import override_settings

    rates = {scope: None for scope in settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})}
    loggers = [logging.getLogger(name) for name in QUIET_LOGGERS]
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.WARNING)
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}):
            yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


class InProcessClient:
    """The platform's own public API through Django's request handler (no network)."""

    def __init__(self):
        from django.test import Client

        self.client = Client(HTTP_HOST=_host(), HTTP_ACCEPT="application/json")

    def get(self, path: str, params=None):
        response = self.client.get(f"{path}" + (f"?{query_string(params)}" if params else ""))
        return response.status_code, _parse(response.content)

    def post(self, path: str, body: bytes):
        response = self.client.post(path, data=body, content_type="application/json")
        return response.status_code, _parse(response.content)
