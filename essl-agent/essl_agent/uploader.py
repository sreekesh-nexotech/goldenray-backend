"""Outbound HTTPS client for the platform's agent protocol (``/api/agent/<version>/``, service-token auth).

Only outbound connections are made: the office needs no static address, no port forward and no inbound rule. A
failure never discards data — the caller keeps every record queued until the platform confirms it. Uploads carry an
``Idempotency-Key`` derived from their content, so a retry after a lost answer is replayed, not re-applied.

Errors: :class:`AuthRejected` (401/403: the credential was rotated or revoked — an operator must act),
:class:`Refused` (another 4xx with the platform's error ``code``, e.g. ``device_bound_elsewhere``: retrying the same
request cannot help), :class:`ServerUnavailable` (network, 429, 5xx: transient, retried with backoff).
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from essl_agent import VERSION


class ServerUnavailable(RuntimeError):
    """Transient: the platform could not be reached or failed the request."""


class AuthRejected(RuntimeError):
    """Permanent until an operator acts: the credential was refused."""


class Refused(RuntimeError):
    """The platform understood the request and said no (4xx other than 401/403/429)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class Uploader:
    def __init__(self, api_base: str, token: str, timeout: int = 60, verify_tls: bool = True):
        self.base = api_base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ctx: ssl.SSLContext | None = None
        if not verify_tls:  # only for a local test server with a self-signed certificate
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # -- transport -------------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, payload: Any = None, *, params: dict | None = None, idempotency_key: str | None = None) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(payload, default=str).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", f"flarize-essl-agent/{VERSION}")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ctx) as response:
                body = response.read()
            return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403):
                raise AuthRejected(f"HTTP {exc.code}: {detail}") from exc
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                try:
                    envelope = json.loads(detail)
                except ValueError:
                    envelope = {}
                if envelope.get("code") == "idempotency_in_progress":  # the first attempt is still running: retry later
                    raise ServerUnavailable(f"HTTP {exc.code}: {detail}") from exc
                raise Refused(exc.code, str(envelope.get("code") or "refused"), str(envelope.get("message") or detail)) from exc
            raise ServerUnavailable(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - network errors of every kind are transient here
            raise ServerUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # -- protocol --------------------------------------------------------------------------------------------------
    def get_config(self) -> dict:
        return self._call("GET", "config/")

    def heartbeat(self, payload: dict) -> dict:
        return self._call("POST", "heartbeat/", payload)

    def announce_device(self, payload: dict) -> dict:
        return self._call("POST", "devices/announce/", payload)

    def report_identity_mismatch(self, payload: dict) -> dict:
        """Evidence only (expected vs reported identity, address, when): no users and no attendance go with it."""
        return self._call("POST", "devices/identity-mismatch/", payload)

    def report_discovery(self, payload: dict) -> dict:
        """What answered on the ZK port and what each said it was; the platform decides which device, if any."""
        return self._call("POST", "devices/discovery/", payload)

    def upload_users(self, device: str | None, serial: str | None, users: list[dict], *, read_at: str | None, idempotency_key: str) -> dict:
        return self._call("POST", "sync/users/", {"device": device, "serial_number": serial, "users": users, "read_at": read_at}, idempotency_key=idempotency_key)

    def upload_attendance(self, device: str | None, serial: str | None, records: list[dict], *, batch_id: str, idempotency_key: str) -> dict:
        return self._call("POST", "sync/attendance/", {"device": device, "serial_number": serial, "batch_id": batch_id, "records": records}, idempotency_key=idempotency_key)

    def sync_status(self, device: str | None = None, serial: str | None = None) -> dict:
        return self._call("GET", "sync-status/", params={"device": device, "serial_number": serial})
