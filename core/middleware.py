"""Request context middleware: request id + one structured access-log line per request."""

from __future__ import annotations

import logging
import time
import uuid

from flarize.logging import redact_path, request_id_var, user_uid_var

logger = logging.getLogger("flarize.request")

REQUEST_ID_HEADER = "X-Request-ID"


def _valid_request_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


class RequestIdMiddleware:
    """Accept an incoming ``X-Request-ID`` (UUID, set by nginx) or mint one; echo it on the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _valid_request_id(request.headers.get(REQUEST_ID_HEADER)) or str(uuid.uuid4())
        request.request_id = request_id
        request_token = request_id_var.set(request_id)
        user_token = user_uid_var.set(None)
        started = time.monotonic()
        try:
            response = self.get_response(request)
            user = getattr(request, "user", None)
            user_uid = str(getattr(user, "uid", "")) if user is not None and getattr(user, "is_authenticated", False) else None
            if user_uid:
                user_uid_var.set(user_uid)
            response[REQUEST_ID_HEADER] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": redact_path(request.path),
                    "status": response.status_code,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return response
        finally:
            user_uid_var.reset(user_token)
            request_id_var.reset(request_token)
