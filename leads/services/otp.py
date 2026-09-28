"""One-time codes for website forms (PLAN §3.3 ``otp/send``, ``otp/verify``) and the verification token.

* :func:`send_code` — Indian mobile numbers only (the caller normalises to E.164). Besides the ``otp`` (per phone)
  and ``otp_ip`` (per client IP) throttles, a database cap of ``LEADS_OTP_MAX_SENDS_PER_PHONE_PER_DAY`` sends per
  number per 24 h holds even when the cache is down (throttles fail open). The provider call runs *outside* any
  database transaction (no row locks held during network I/O); the ``leads_otp_request`` row is written after Twilio
  accepted the send.
* :func:`verify_code` — the latest live code for the number and purpose; each check is counted with a conditional
  ``UPDATE`` (at most ``LEADS_OTP_MAX_ATTEMPTS``, so concurrent guesses cannot exceed it). On approval the row is
  marked verified and a **verification token** is returned.
* The token is a ``django.core.signing`` value (salted, timestamped) carrying the E.164 number, the purpose and the
  verification time; it is valid for ``LEADS_VERIFICATION_TOKEN_TTL_SECONDS`` and only for that number and purpose.
  ``POST leads`` requires it for every submission that carries a phone number (:func:`read_token`).

Provider failures never leak Twilio's text: 503 ``otp_unavailable`` (try later), 429 ``otp_limit_reached``, 400
``otp_send_failed`` / ``otp_invalid`` / ``otp_expired``, 429 ``otp_attempts_exceeded``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from audit.services import record
from core.errors import DomainError
from leads.models import OtpRequest
from leads.services import twilio_verify
from leads.services.twilio_verify import ProviderRejected, ProviderUnavailable, VerificationNotFound

TOKEN_SALT = "leads.otp-verification"
PUBLIC = "CUSTOMER"  # audit actor kind of anonymous website visitors


@dataclass(frozen=True)
class Verification:
    token: str
    expires_at: dt.datetime
    verified_at: dt.datetime


def _unavailable() -> DomainError:
    return DomainError("otp_unavailable", "We could not send or check the code right now. Please try again in a few minutes.", status=503)


def _limit() -> DomainError:
    return DomainError("otp_limit_reached", "Too many codes were requested for this number. Please try again later.", status=429)


def send_code(*, phone_e164: str, purpose: str = OtpRequest.Purpose.LEAD, ip: str | None = None) -> OtpRequest:
    now = timezone.now()
    if OtpRequest.objects.filter(phone_e164=phone_e164, created_at__gte=now - timedelta(days=1)).count() >= settings.LEADS_OTP_MAX_SENDS_PER_PHONE_PER_DAY:
        raise _limit()
    try:
        result = twilio_verify.client().send(phone_e164)
    except ProviderUnavailable:
        raise _unavailable() from None
    except ProviderRejected as exc:
        if exc.status == 429:
            raise _limit() from None
        raise DomainError("otp_send_failed", "We could not send a code to this number.", errors={"phone": ["We could not send a code to this number."]}) from None
    with transaction.atomic():
        otp = OtpRequest.objects.create(phone_e164=phone_e164, purpose=purpose, provider_sid=result.sid[:64], expires_at=now + timedelta(seconds=settings.LEADS_OTP_TTL_SECONDS), ip=ip, created_at=now)
        record("leads.otp_sent", object_type="leads.otprequest", actor_kind=PUBLIC, after={"phone": phone_e164, "purpose": purpose, "provider_sid": otp.provider_sid})
    return otp


def _expire(otp: OtpRequest) -> None:
    OtpRequest.objects.filter(pk=otp.pk).update(expires_at=F("created_at") + timedelta(microseconds=1))


def verify_code(*, phone_e164: str, code: str, purpose: str = OtpRequest.Purpose.LEAD, ip: str | None = None) -> Verification:
    now = timezone.now()
    otp = OtpRequest.objects.filter(phone_e164=phone_e164, purpose=purpose, verified_at__isnull=True, expires_at__gt=now).order_by("-created_at", "-id").first()
    if otp is None:
        raise DomainError("otp_expired", "This code has expired or was never sent. Request a new code.", errors={"code": ["Request a new code."]})
    counted = OtpRequest.objects.filter(pk=otp.pk, verified_at__isnull=True, attempts__lt=settings.LEADS_OTP_MAX_ATTEMPTS).update(attempts=F("attempts") + 1)
    if not counted:
        raise DomainError("otp_attempts_exceeded", "Too many wrong codes. Request a new code.", status=429)
    try:
        result = twilio_verify.client().check(phone_e164, code)
    except VerificationNotFound:
        _expire(otp)
        raise DomainError("otp_expired", "This code has expired. Request a new code.", errors={"code": ["Request a new code."]}) from None
    except ProviderUnavailable:
        raise _unavailable() from None
    except ProviderRejected as exc:
        if exc.status == 429:
            _expire(otp)
            raise DomainError("otp_attempts_exceeded", "Too many wrong codes. Request a new code.", status=429) from None
        raise DomainError("otp_invalid", "The code is not correct.", errors={"code": ["The code is not correct."]}) from None
    if not result.approved:
        raise DomainError("otp_invalid", "The code is not correct.", errors={"code": ["The code is not correct."]})
    verified_at = timezone.now()
    with transaction.atomic():
        OtpRequest.objects.filter(pk=otp.pk).update(verified_at=verified_at)
        record("leads.otp_verified", object_type="leads.otprequest", actor_kind=PUBLIC, after={"phone": phone_e164, "purpose": purpose})
    token = signing.dumps({"p": phone_e164, "u": purpose, "t": verified_at.isoformat()}, salt=TOKEN_SALT, compress=True)
    return Verification(token=token, expires_at=verified_at + timedelta(seconds=settings.LEADS_VERIFICATION_TOKEN_TTL_SECONDS), verified_at=verified_at)


def read_token(token: str, *, phone_e164: str, purpose: str = OtpRequest.Purpose.LEAD) -> dt.datetime:
    """The verification time proven by ``token`` for this number and purpose, or a 400 ``verification_*`` error."""
    field = {"verification_token": ["Verify your phone number with the code we send you."]}
    if not token:
        raise DomainError("verification_required", "Verify your phone number with the one-time code first.", errors=field)
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=settings.LEADS_VERIFICATION_TOKEN_TTL_SECONDS)
    except signing.SignatureExpired:
        raise DomainError("verification_expired", "Your phone verification has expired. Request a new code.", errors=field) from None
    except signing.BadSignature:
        raise DomainError("verification_invalid", "The phone verification is not valid.", errors=field) from None
    if not isinstance(data, dict) or data.get("p") != phone_e164 or data.get("u") != purpose:
        raise DomainError("verification_mismatch", "The phone verification was made for another number.", errors=field)
    return dt.datetime.fromisoformat(data["t"])
