"""Structured JSON logging with request context.

``core.middleware.RequestIdMiddleware`` sets the context variables below for the duration of a request; Celery
tasks may set them too. Every log record then carries ``request_id`` (and ``user_uid`` when known), and the
formatter writes one JSON object per line to stdout.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone

REDACTED = "[redacted]"

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


# Path segments that *are* the credential (the URL is the capability): signed media URLs (reusable for 10 minutes),
# single-use document links, per-device terminal tokens (long-lived), customer link tokens and blog preview links. They are replaced by
# REDACTED wherever a path is logged or stored — access log, security log, SystemException.path and any message that
# embeds a path (Django's "Internal Server Error: <path>"). deploy/nginx/flarize.conf applies the same rules to its
# access log ($loggable_uri).
_CAPABILITY_SEGMENTS = (
    re.compile(r"(/api/[^/\s]+/(?:media|documents)/download/)[^/\s?#]+(?=/)"),
    re.compile(r"(/iclock/)[^/\s?#]+(?=/)"),
    re.compile(r"(/api/customer/[^/\s]+/[^/\s?#]+/)[^/\s?#]+(?=/)"),
    re.compile(r"(/api/public/[^/\s]+/content/preview/)[^/\s?#]+(?=/)"),
)


def redact_path(text):
    """``text`` (a path, or a message containing paths) with every capability token replaced by ``[redacted]``."""
    if not isinstance(text, str) or "/" not in text:
        return text
    for pattern in _CAPABILITY_SEGMENTS:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
    return text


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


# Celery logs every received/succeeded/failed task with a ``data`` extra holding the task's ``args`` and ``kwargs``
# (``celery.worker.strategy``, ``celery.app.trace``). Task arguments may carry personal data or one-time links, so
# they never reach a log line; the task name and id identify the run.
_TASK_ARGUMENT_KEYS = ("args", "kwargs")


def _redact_task_data(value):
    if isinstance(value, dict) and any(key in value for key in _TASK_ARGUMENT_KEYS):
        return {key: (REDACTED if key in _TASK_ARGUMENT_KEYS else item) for key, item in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises: unserialisable extras are rendered with ``str``."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_path(record.getMessage()),
            "request_id": getattr(record, "request_id", None),
        }
        user_uid = getattr(record, "user_uid", None)
        if user_uid:
            payload["user_uid"] = user_uid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact_task_data(value) if key == "data" else redact_path(value) if key == "path" else value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)
