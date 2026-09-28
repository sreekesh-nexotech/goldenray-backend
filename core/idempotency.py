"""``Idempotency-Key`` replay store (PLAN §3.1): public POSTs (leads, OTP) and machine POSTs (agent uploads).

The first request with a key runs normally; its response (status + body) is stored for 24 h under a key scoped by
endpoint and caller. A retry with the same key and the same body replays the stored response with
``Idempotent-Replayed: true``. The same key with a different body → 422 ``idempotency_key_reused``; a retry while
the first request is still running → 409 ``idempotency_in_progress``. 5xx responses are not stored, so the client
may retry them.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import UploadedFile
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

from core.errors import Conflict, DomainError
from flarize.client_ip import get_client_ip

HEADER = "Idempotency-Key"
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
_LOCK_TTL_SECONDS = 60


def _caller(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"u:{user.pk}"
    return f"ip:{get_client_ip(request)}"


def _fingerprint(request) -> str:
    def encode(value):
        if isinstance(value, UploadedFile):
            return {"file": value.name, "size": value.size}
        return str(value)

    data = request.data
    if hasattr(data, "lists"):
        body = {key: [encode(item) for item in values] for key, values in data.lists()}
    else:
        body = data
    material = json.dumps(body, cls=JSONEncoder, sort_keys=True, default=encode)
    return hashlib.sha256(material.encode()).hexdigest()


def _cache_key(scope: str, caller: str, key: str) -> str:
    digest = hashlib.sha256(f"{scope}|{caller}|{key}".encode()).hexdigest()
    return f"idem:{digest}"


def idempotent(scope: str, *, required: bool = False):
    """Decorator for a DRF view method (``post``/``create``)."""

    def decorator(method):
        @wraps(method)
        def wrapper(view, request, *args, **kwargs):
            key = request.headers.get(HEADER)
            if not key:
                if required:
                    raise DomainError("idempotency_key_required", f"The {HEADER} header is required.", errors={HEADER: ["This header is required."]})
                return method(view, request, *args, **kwargs)
            if not _KEY_RE.match(key):
                raise DomainError("invalid_idempotency_key", f"{HEADER} must be 8-128 characters of letters, digits, '-', '_', ':' or '.'.", errors={HEADER: ["Invalid format."]})

            store_key = _cache_key(scope, _caller(request), key)
            fingerprint = _fingerprint(request)
            stored = cache.get(store_key)
            if stored is not None:
                if stored["fingerprint"] != fingerprint:
                    raise DomainError("idempotency_key_reused", f"This {HEADER} was already used with a different request body.", status=422)
                response = Response(stored["data"], status=stored["status"])
                response["Idempotent-Replayed"] = "true"
                return response

            lock_key = f"{store_key}:lock"
            if not cache.add(lock_key, 1, _LOCK_TTL_SECONDS):
                raise Conflict("idempotency_in_progress", "A request with this Idempotency-Key is still being processed.")
            try:
                response = method(view, request, *args, **kwargs)
                if response.status_code < 500:
                    data = json.loads(json.dumps(response.data, cls=JSONEncoder)) if response.data is not None else None
                    cache.set(store_key, {"fingerprint": fingerprint, "status": response.status_code, "data": data}, int(getattr(settings, "IDEMPOTENCY_TTL_SECONDS", 86400)))
                return response
            finally:
                cache.delete(lock_key)

        return wrapper

    return decorator
