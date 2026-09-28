"""``AuditMiddleware``: opens the audit context (request id, client IP) for every request.

* ``X-Request-ID`` is honoured when it is a well-formed UUID (nginx sets one), otherwise a new ``uuid4`` is minted.
  When ``core.middleware.RequestIdMiddleware`` runs first (it does in every settings module) its id is reused, so the
  access log, error log and audit rows of one request always share the same id.
* The client IP comes from ``flarize.client_ip`` — ``X-Forwarded-For`` is trusted only from ``TRUSTED_PROXIES``.
* The response echoes ``X-Request-ID``.
* The actor is attached later, by the authentication class, once DRF has authenticated the request.
"""

from __future__ import annotations

import uuid

from audit import context
from flarize.client_ip import get_client_ip

REQUEST_ID_HEADER = "X-Request-ID"


def parse_request_id(value) -> str | None:
    """Canonical UUID string for a well-formed id, else ``None``."""
    if not value or not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = parse_request_id(getattr(request, "request_id", None)) or parse_request_id(request.headers.get(REQUEST_ID_HEADER)) or str(uuid.uuid4())
        request.request_id = request_id
        token = context.begin(request_id=request_id, ip=get_client_ip(request) or None)
        try:
            response = self.get_response(request)
        finally:
            context.end(token)
        if not response.has_header(REQUEST_ID_HEADER):
            response[REQUEST_ID_HEADER] = request_id
        return response
