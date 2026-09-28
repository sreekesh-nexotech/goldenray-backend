"""Thin Twilio Verify client (PLAN §3.3 ``otp/send``, ``otp/verify``) — the only code that talks to Twilio.

Backends (``settings.LEADS_OTP_BACKEND``):

* ``twilio`` (staging/prod): ``requests`` to ``https://verify.twilio.com/v2`` with HTTP Basic auth. Configuration comes
  from the enabled ``TWILIO`` integration (``company_integration``, Fernet encrypted, Admin-editable) through
  :mod:`core.integrations`, else from the ``TWILIO_*`` environment settings. Missing configuration fails closed
  (:class:`ProviderUnavailable`); so do Twilio answers that blame *our* configuration (401 credentials, 404 on the
  Verify service) — logged at ERROR, because they block every visitor. Twilio's own error text is never passed to the
  website.
* ``fake`` (dev/test): no network. Every send is recorded in :data:`SENT` and a pending verification is kept in the
  cache (so it works across dev workers); the code ``000000`` approves it, anything else does not.

Twilio generates, stores and expires the code; nothing here sees it except the one check request.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from core import integrations

logger = logging.getLogger("flarize.leads.otp")

BASE_URL = "https://verify.twilio.com/v2"
FAKE_APPROVED_CODE = "000000"
SENT: list[dict] = []  # fake backend: every send, for tests and the dev console


class VerifyError(RuntimeError):
    """Twilio Verify could not complete the request."""


class ProviderUnavailable(VerifyError):
    """Not configured, unreachable, or a Twilio-side failure (5xx): try again later."""


class ProviderRejected(VerifyError):
    """Twilio refused the request (4xx): invalid number, too many sends or checks, …"""

    def __init__(self, message: str, *, status: int, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class VerificationNotFound(ProviderRejected):
    """No pending verification for the number (expired, already approved, or too many wrong codes)."""


@dataclass(frozen=True)
class SendResult:
    sid: str
    status: str


@dataclass(frozen=True)
class CheckResult:
    sid: str
    status: str

    @property
    def approved(self) -> bool:
        return self.status == "approved"


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str
    verify_service_sid: str

    def __repr__(self) -> str:  # never print the auth token
        return f"TwilioConfig(account_sid={self.account_sid!r}, verify_service_sid={self.verify_service_sid!r})"


def load_config() -> TwilioConfig:
    stored = integrations.get_config(integrations.TWILIO)
    if stored:
        values = {"account_sid": stored.get("account_sid", ""), "auth_token": stored.get("auth_token", ""), "verify_service_sid": stored.get("verify_service_sid", "")}
    else:
        values = {"account_sid": settings.TWILIO_ACCOUNT_SID, "auth_token": settings.TWILIO_AUTH_TOKEN, "verify_service_sid": settings.TWILIO_VERIFY_SERVICE_SID}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ProviderUnavailable(f"Twilio Verify is not configured (missing: {', '.join(missing)}).")
    return TwilioConfig(**values)


class TwilioVerifyClient:
    def __init__(self, config: TwilioConfig, *, session: requests.Session | None = None, timeout: float | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout if timeout is not None else settings.TWILIO_TIMEOUT_SECONDS

    def _url(self, resource: str) -> str:
        return f"{BASE_URL}/Services/{self.config.verify_service_sid}/{resource}"

    def _post(self, resource: str, data: dict) -> dict:
        try:
            response = self.session.post(self._url(resource), data=data, auth=(self.config.account_sid, self.config.auth_token), timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"Twilio Verify unreachable: {exc.__class__.__name__}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code >= 500:
            raise ProviderUnavailable(f"Twilio Verify error {response.status_code}.")
        if response.status_code == 401 or (response.status_code == 404 and resource != "VerificationCheck"):
            # Our side is wrong (rotated/invalid credentials, unknown Verify service): an outage for every visitor,
            # never a problem with their number or code — and the operators must hear about it.
            logger.error("Twilio Verify rejected the configuration (%s, code %s) on %s", response.status_code, body.get("code"), resource)
            raise ProviderUnavailable(f"Twilio Verify configuration error ({response.status_code}, code {body.get('code')}).")
        if response.status_code == 404 and resource == "VerificationCheck":
            raise VerificationNotFound("No pending verification for this number.", status=404, code=body.get("code"))
        if response.status_code >= 400:
            raise ProviderRejected(f"Twilio Verify refused the request ({response.status_code}, code {body.get('code')}).", status=response.status_code, code=body.get("code"))
        return body

    def send(self, phone_e164: str, channel: str = "sms") -> SendResult:
        body = self._post("Verifications", {"To": phone_e164, "Channel": channel})
        return SendResult(sid=str(body.get("sid", "")), status=str(body.get("status", "")))

    def check(self, phone_e164: str, code: str) -> CheckResult:
        body = self._post("VerificationCheck", {"To": phone_e164, "Code": code})
        return CheckResult(sid=str(body.get("sid", "")), status=str(body.get("status", "")))


class FakeVerifyClient:
    """Dev/test backend: accepts :data:`FAKE_APPROVED_CODE` for a number with a pending (sent) verification."""

    ttl = 600

    @staticmethod
    def _key(phone_e164: str) -> str:
        return f"leads:fake-verify:{phone_e164}"

    def send(self, phone_e164: str, channel: str = "sms") -> SendResult:
        sid = "VE" + uuid.uuid4().hex
        cache.set(self._key(phone_e164), sid, self.ttl)
        SENT.append({"to": phone_e164, "channel": channel, "sid": sid})
        logger.info("fake OTP sent (code %s)", FAKE_APPROVED_CODE, extra={"sid": sid})
        return SendResult(sid=sid, status="pending")

    def check(self, phone_e164: str, code: str) -> CheckResult:
        sid = cache.get(self._key(phone_e164))
        if sid is None:
            raise VerificationNotFound("No pending verification for this number.", status=404, code=20404)
        if code != FAKE_APPROVED_CODE:
            return CheckResult(sid=sid, status="pending")
        cache.delete(self._key(phone_e164))
        return CheckResult(sid=sid, status="approved")


def client():
    backend = settings.LEADS_OTP_BACKEND
    if backend == "fake":
        return FakeVerifyClient()
    if backend == "twilio":
        return TwilioVerifyClient(load_config())
    raise ProviderUnavailable(f"Unknown LEADS_OTP_BACKEND {backend!r}.")
