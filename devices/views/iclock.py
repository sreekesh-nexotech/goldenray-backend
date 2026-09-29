"""``/iclock/<device_token>/{cdata,getrequest,devicecmd,registry,ping,…}`` — the ADMS push receiver (plain Django views).

Behind the ``ADMS_RECEIVER`` flag (404 while off, PLAN D-10). CSRF-exempt, outside DRF authentication: the terminal
authenticates by the secret path segment **and** the serial it states (:mod:`devices.services.adms`). Every reply is
HTTP 200 ``text/plain; charset=utf-8`` (ASCII bodies, the eSSL header) with an explicit ``Content-Length`` — firmware keeps the connection alive and chokes on
chunked replies. The body is read in chunks and at most ``MAX_STORED_BODY_BYTES`` (1 MiB) is kept; the rest is drained
and counted. Each device token may send ``iclock`` (300/min) requests; beyond that the terminal is answered ``ERROR``
(nothing recorded) so it retries later rather than losing a push.
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from core.flags import require_flag
from devices.models.adms import MAX_STORED_BODY_BYTES
from devices.services import adms
from devices.services.common import normalize_ip
from flarize.client_ip import get_client_ip
from flarize.logging import redact_path
from flarize.throttles import parse_rate

CHUNK_BYTES = 64 * 1024
ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE")


def plain(text: str) -> HttpResponse:
    payload = text.encode("ascii", errors="replace")
    response = HttpResponse(payload, status=200, content_type="text/plain; charset=utf-8")  # as eSSL answered
    response["Content-Length"] = str(len(payload))
    return response


def read_body(request) -> tuple[bytes, int, bool]:
    """``(kept bytes, total size, truncated)`` — a hostile peer never decides how much memory is used."""
    kept, total = bytearray(), 0
    while True:
        chunk = request.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        room = MAX_STORED_BODY_BYTES - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
    return bytes(kept), total, total > len(kept)


def throttled(token: str) -> bool:
    """The ``iclock`` rate per device token (fails open on a cache outage, like every throttle here)."""
    from rest_framework.settings import api_settings

    count, duration = parse_rate(api_settings.DEFAULT_THROTTLE_RATES.get("iclock"))
    if not count:
        return False
    key = f"throttle_iclock_{hashlib.sha256(token.encode()).hexdigest()[:32]}"
    try:
        if cache.add(key, 1, duration):
            return False
        return cache.incr(key) > count
    except Exception:  # noqa: BLE001 - fail open (nginx limit_req is the backstop)
        return False


@csrf_exempt
@require_flag("ADMS_RECEIVER")
def receive(request, device_token: str, endpoint: str = ""):
    if request.method not in ALLOWED_METHODS:
        return plain(adms.OK)
    if throttled(device_token):
        return plain(adms.ERROR)
    body, size, truncated = read_body(request)
    incoming = adms.Incoming(
        token=device_token,
        endpoint=endpoint,
        method=request.method,
        path=redact_path(request.path),
        raw_query=request.META.get("QUERY_STRING", ""),
        query={key: request.GET.getlist(key) for key in request.GET.keys()},
        headers={key: value for key, value in request.headers.items()},
        body=body,
        body_bytes=size,
        truncated=truncated,
        client_ip=normalize_ip(get_client_ip(request)),
        peer_ip=normalize_ip(request.META.get("REMOTE_ADDR")),
        content_type=request.META.get("CONTENT_TYPE", ""),
    )
    return plain(adms.receive(incoming))
