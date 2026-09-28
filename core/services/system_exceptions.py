"""Never-raising writer for ``core.SystemException`` (standard §5 "two error sinks")."""

from __future__ import annotations

import logging
import traceback as traceback_module
import uuid

from django.db import transaction

from flarize.logging import current_request_id

logger = logging.getLogger("flarize.errors")

_MAX_MESSAGE = 4000
_MAX_TRACEBACK = 20000


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def record_exception(exc: BaseException, *, request=None, source: str = "api", context: dict | None = None) -> None:
    """Store ``exc`` in ``core_system_exception``. Swallows every error (logging it) — callers never see a failure."""
    try:
        from core.models import SystemException

        django_request = getattr(request, "_request", request)
        user = getattr(django_request, "user", None) if django_request is not None else None
        user_uid = getattr(user, "uid", None) if user is not None and getattr(user, "is_authenticated", False) else None
        request_id = getattr(django_request, "request_id", None) or current_request_id()
        tb = "".join(traceback_module.format_exception(type(exc), exc, exc.__traceback__))
        with transaction.atomic():
            SystemException.objects.create(
                source=source,
                request_id=_uuid_or_none(request_id),
                method=(getattr(django_request, "method", "") or "")[:8],
                path=(getattr(django_request, "path", "") or "")[:512],
                user_uid=_uuid_or_none(user_uid),
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}"[:255],
                message=str(exc)[:_MAX_MESSAGE],
                traceback=tb[-_MAX_TRACEBACK:],
                context=context or {},
            )
    except Exception:  # noqa: BLE001 - this writer must never raise
        logger.warning("could not record SystemException", exc_info=True)
