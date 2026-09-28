"""Serving private files after the caller has been authorised (signed URL, one-time document link).

With ``USE_X_ACCEL`` (staging/prod) Django answers with an empty body and ``X-Accel-Redirect`` to nginx's
``internal`` location ``/media/private/`` (aliasing ``PRIVATE_MEDIA_ROOT``), so the file is streamed by nginx and no
worker thread is held. Otherwise (dev, tests) the file is streamed with ``FileResponse``.

Every private response is ``Cache-Control: private, no-store``, ``X-Content-Type-Options: nosniff`` and sandboxed by
CSP, so a crafted file can never run script in the site's origin.
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponse

from core.errors import NotFound
from media.services.storage import StorageNotFound, private_storage

INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"})


def _content_disposition(filename: str, inline: bool) -> str:
    fallback = "".join(char if char.isascii() and char.isprintable() and char not in '"\\' else "_" for char in filename) or "download"
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def private_file_response(key: str, *, content_type: str, filename: str, inline: bool | None = None) -> HttpResponse:
    storage = private_storage()
    inline = content_type in INLINE_TYPES if inline is None else inline
    if settings.USE_X_ACCEL:
        if not storage.exists(key):
            raise NotFound("not_found", "The file no longer exists.")
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = settings.X_ACCEL_PRIVATE_PREFIX + quote(key)
    else:
        try:
            handle = storage.open(key)
        except StorageNotFound:
            raise NotFound("not_found", "The file no longer exists.") from None
        response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = _content_disposition(filename, inline)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
