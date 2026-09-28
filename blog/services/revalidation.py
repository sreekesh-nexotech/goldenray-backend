"""Thin client for the website's on-demand revalidation route (Next.js ``/api/revalidate``; PLAN §3.5).

The frontend's existing contract is kept: one ``POST`` per path with the JSON body ``{"secret": …, "path": …}``
(``revalidatePath(path)``). The URL and the shared secret come from the company profile (``blog_revalidate_url``,
``blog_revalidate_secret`` — Fernet-encrypted, write-only). Legacy weaknesses fixed (CMS_BLUEPRINT §13/§17 #2–#4):

* it runs from outbox handlers, i.e. **after commit** (never for a write that rolled back);
* every request also carries ``X-Flarize-Timestamp`` and ``X-Flarize-Signature: sha256=<HMAC(secret, "<ts>.<body>")>``
  so the frontend can move from the body secret to a signature check; non-2xx responses and network errors are
  logged (a wrong secret no longer fails silently);
* duplicate paths are sent once; events are deduplicated per entry version at emit time;
* one call is bounded (it runs inside the outbox drainer's Celery time limit): the first transport failure ends the
  batch and ``BLOG_REVALIDATE_BUDGET_SECONDS`` (default 20) caps its wall-clock time; skipped paths are logged.

It is **fail-soft**: nothing raises — the website falls back to its ISR window. No URL or no secret disables it.
Backends: ``http`` (default), ``fake`` (tests: records requests in :data:`FakeBackend.sent`), ``off``
(``settings.BLOG_REVALIDATE_BACKEND``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

import requests
from django.conf import settings

from flarize.crypto import DecryptionError

logger = logging.getLogger("flarize.blog.revalidation")


@dataclass(frozen=True)
class Result:
    path: str
    status: int | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class HttpBackend:
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        timeout = float(getattr(settings, "BLOG_REVALIDATE_TIMEOUT_SECONDS", 3))
        response = requests.post(url, data=body, headers=headers, timeout=timeout, allow_redirects=False)
        return response.status_code


@dataclass
class FakeBackend:
    """Records requests instead of sending them; ``status`` is what every request answers."""

    status: int = 200
    sent: list[dict] = field(default_factory=list)
    fail_with: type[Exception] | None = None

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        if self.fail_with is not None:
            raise self.fail_with("fake failure")
        self.sent.append({"url": url, "body": json.loads(body), "headers": dict(headers)})
        return self.status


FAKE = FakeBackend()


def backend():
    name = getattr(settings, "BLOG_REVALIDATE_BACKEND", "http")
    if name == "fake":
        return FAKE
    if name == "off":
        return None
    return HttpBackend()


def _target() -> tuple[str, str]:
    """``(url, secret)`` from the company profile; blanks when unset or when the secret cannot be decrypted."""
    from company.services.profile import current_profile

    try:
        profile = current_profile()
    except DecryptionError:  # fail-closed: never ping without the secret, never break the outbox drain
        logger.error("blog revalidation secret cannot be decrypted; revalidation disabled")
        return "", ""
    return profile.blog_revalidate_url or "", profile.blog_revalidate_secret or ""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _site_path(path) -> bool:
    return isinstance(path, str) and path.startswith("/") and not path.startswith("//") and len(path) <= 1000


def budget_seconds() -> float:
    """Wall-clock budget of one :func:`revalidate` call (it runs inside the outbox drainer's Celery time limit)."""
    return float(getattr(settings, "BLOG_REVALIDATE_BUDGET_SECONDS", 20))


def _skipped(remaining: int, why: str, reason: str) -> None:
    if remaining:
        logger.warning(f"blog revalidation {why}: {remaining} paths skipped (the website's ISR window catches up)", extra={"reason": reason, "skipped": remaining})


def revalidate(paths, *, reason: str = "") -> list[Result]:
    """Ask the website to rebuild ``paths`` (site-relative). Never raises.

    The call is bounded: it runs inside the outbox drainer (a Celery task with a hard time limit), so the first
    transport failure (connection refused, timeout — the website is down, every other path would wait just as long)
    ends the batch, and so does exhausting :func:`budget_seconds`. Skipped paths are logged, never retried: the
    website's ISR window refreshes them anyway.
    """
    client = backend()
    url, secret = _target()
    wanted = [path for path in dict.fromkeys(paths or []) if _site_path(path)]
    if client is None or not url or not secret or not wanted:
        return []
    results = []
    deadline = time.monotonic() + budget_seconds()
    for index, path in enumerate(wanted):
        if index and time.monotonic() >= deadline:
            _skipped(len(wanted) - index, "time budget exhausted", reason)
            break
        body = json.dumps({"secret": secret, "path": path}, separators=(",", ":")).encode()
        timestamp = int(time.time())
        headers = {"Content-Type": "application/json", "X-Flarize-Timestamp": str(timestamp), "X-Flarize-Signature": f"sha256={sign(secret, timestamp, body)}"}
        try:
            status = client.post(url, body, headers)
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            logger.warning("blog revalidation request failed", extra={"path": path, "reason": reason, "error": type(exc).__name__})
            results.append(Result(path=path, status=None, error=type(exc).__name__))
            _skipped(len(wanted) - index - 1, "stopped after a transport failure", reason)
            break
        result = Result(path=path, status=status)
        if not result.ok:
            logger.warning("blog revalidation rejected", extra={"path": path, "reason": reason, "status": status})
        results.append(result)
    return results
