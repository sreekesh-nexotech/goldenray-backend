"""Structured JSON logging with request context.

``core.middleware.RequestIdMiddleware`` sets the context variables below for the duration of a request; Celery
tasks may set them too. Every log record then carries ``request_id`` (and ``user_uid`` when known), and the
formatter writes one JSON object per line to stdout.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str | None] = ContextVar("flarize_request_id", default=None)
user_uid_var: ContextVar[str | None] = ContextVar("flarize_user_uid", default=None)

# Attributes every LogRecord has; anything else passed through ``extra=`` is emitted as a field.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "request_id",
        "user_uid",
    }
)


def current_request_id() -> str | None:
    return request_id_var.get()


class RequestContextFilter(logging.Filter):
    """Attach the current request id and user uid to every record.

    Django's handler logs some ``django.request`` records after ``RequestIdMiddleware`` returned (and reset the
    context variable); those carry the request itself, whose ``request_id`` the middleware stamped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id_var.get() or getattr(getattr(record, "request", None), "request_id", None)
        if not hasattr(record, "user_uid"):
            record.user_uid = user_uid_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises: unserialisable extras are rendered with ``str``."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        user_uid = getattr(record, "user_uid", None)
        if user_uid:
            payload["user_uid"] = user_uid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)
