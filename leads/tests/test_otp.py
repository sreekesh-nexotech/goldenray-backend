"""POST /api/public/v1/otp/send/ and otp/verify/ — fake Twilio backend, throttles, caps, token, idempotency."""

import datetime as dt

import pytest
from django.core import signing
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from audit.models import AuditLog
from core.errors import DomainError
from leads.models import OtpRequest
from leads.services import otp, twilio_verify
from leads.services.twilio_verify import ProviderRejected, ProviderUnavailable, VerificationNotFound
from leads.tests.factories import OtpRequestFactory

pytestmark = pytest.mark.django_db
SEND = "/api/public/v1/otp/send/"
VERIFY = "/api/public/v1/otp/verify/"


def _send(client, phone="9876543210", **extra):
    return client.post(SEND, {"phone": phone}, format="json", **extra)


def _verify(client, phone="9876543210", code="000000", **extra):
    return client.post(VERIFY, {"phone": phone, "code": code}, format="json", **extra)


class TestSend:
    def test_shape_and_row(self, api_client):
        response = _send(api_client, "+91 98765-43210")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pending" and body["phone"] == "+919876543210" and body["expires_at"]
        row = OtpRequest.objects.get()
        assert (row.phone_e164, row.purpose, row.attempts, row.verified_at) == ("+919876543210", "LEAD", 0, None)
        assert row.provider_sid.startswith("VE") and row.ip == "127.0.0.1"
        assert twilio_verify.SENT == [{"to": "+919876543210", "channel": "sms", "sid": row.provider_sid}]
        assert AuditLog.objects.get(action="leads.otp_sent").actor_kind == "CUSTOMER"

    def test_anonymous_without_authentication(self):
        from leads.views.public import OtpSendView, OtpVerifyView
        from leads.views.throttles import OtpIpThrottle, OtpPhoneThrottle, OtpVerifyPhoneThrottle

        assert OtpSendView.authentication_classes == [] and OtpSendView.throttle_classes == [OtpPhoneThrottle, OtpIpThrottle]
        assert OtpVerifyView.authentication_classes == [] and OtpVerifyView.throttle_classes == [OtpVerifyPhoneThrottle, OtpIpThrottle]

    @pytest.mark.parametrize("phone", ["", "12345", "5876543210", "+15005550006", "04842000000"])
    def test_only_indian_mobile_numbers(self, api_client, phone):
        response = _send(api_client, phone)
        assert response.status_code == 400 and "phone" in response.json()["errors"]
        assert not OtpRequest.objects.exists() and twilio_verify.SENT == []

    def test_legacy_name_is_accepted_and_not_stored(self, api_client):
        assert api_client.post(SEND, {"phone": "9876543210", "name": "Asha"}, format="json").status_code == 200

    def test_daily_database_cap(self, api_client, settings):
        settings.LEADS_OTP_MAX_SENDS_PER_PHONE_PER_DAY = 2
        OtpRequestFactory.create_batch(2, phone_e164="+919876543210")
        response = _send(api_client)
        assert response.status_code == 429 and response.json()["code"] == "otp_limit_reached"
        with freeze_time(timezone.now() + dt.timedelta(days=1, minutes=1)):
            assert _send(api_client).status_code == 200

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (ProviderUnavailable("down"), 503, "otp_unavailable"),
            (ProviderRejected("too many", status=429, code=60203), 429, "otp_limit_reached"),
            (ProviderRejected("invalid", status=400, code=60200), 400, "otp_send_failed"),
        ],
    )
    def test_provider_errors_never_leak(self, api_client, monkeypatch, error, status, code):
        class Broken:
            def send(self, phone):
                raise error

        monkeypatch.setattr(twilio_verify, "client", lambda: Broken())
        response = _send(api_client)
        assert response.status_code == status and response.json()["code"] == code
        assert "60203" not in response.content.decode() and "60200" not in response.content.decode()
        assert not OtpRequest.objects.exists()

    def test_throttled_per_phone_across_spellings(self, api_client, settings):
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp": "2/10min"}}
        statuses = [_send(api_client, phone).status_code for phone in ("9876543210", "+91 98765 43210", "09876543210")]
        assert statuses == [200, 200, 429]
        assert _send(api_client, "9876543211").status_code == 200  # another number has its own budget

    def test_throttled_per_ip(self, api_client, settings):
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp_ip": "2/10min"}}
        statuses = [_send(api_client, f"98765432{n:02d}").status_code for n in range(3)]
        assert statuses == [200, 200, 429]
        other_ip = api_client.post(SEND, {"phone": "9876543299"}, format="json", REMOTE_ADDR="203.0.113.9")
        assert other_ip.status_code == 200

    def test_idempotency_key_replays_without_a_second_sms(self, api_client):
        first = _send(api_client, HTTP_IDEMPOTENCY_KEY="otp-send-000001")
        second = _send(api_client, HTTP_IDEMPOTENCY_KEY="otp-send-000001")
        assert first.json() == second.json() and second["Idempotent-Replayed"] == "true"
        assert len(twilio_verify.SENT) == 1 and OtpRequest.objects.count() == 1


class TestVerify:
    def test_approved_returns_a_token_for_that_number(self, api_client):
        _send(api_client)
        response = _verify(api_client, "+91 9876543210")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved" and body["verification_token"] and body["expires_at"]
        row = OtpRequest.objects.get()
        assert row.verified_at is not None and row.attempts == 1
        assert otp.read_token(body["verification_token"], phone_e164="+919876543210") == row.verified_at
        assert AuditLog.objects.filter(action="leads.otp_verified").exists()

    def test_wrong_code_counts_attempts_until_the_cap(self, api_client, settings):
        settings.LEADS_OTP_MAX_ATTEMPTS = 2
        _send(api_client)
        first = _verify(api_client, code="111111")
        assert first.status_code == 400 and first.json()["code"] == "otp_invalid" and "code" in first.json()["errors"]
        assert _verify(api_client, code="222222").json()["code"] == "otp_invalid"
        capped = _verify(api_client)  # even the right code: the cap is reached
        assert capped.status_code == 429 and capped.json()["code"] == "otp_attempts_exceeded"
        assert OtpRequest.objects.get().attempts == 2

    def test_nothing_sent_or_expired(self, api_client):
        assert _verify(api_client).json()["code"] == "otp_expired"
        OtpRequestFactory(phone_e164="+919876543210", created_at=timezone.now() - dt.timedelta(minutes=20), expires_at=timezone.now() - dt.timedelta(minutes=10))
        assert _verify(api_client).json()["code"] == "otp_expired"

    def test_provider_lost_the_verification(self, api_client):
        OtpRequestFactory(phone_e164="+919876543210")  # our row exists, Twilio has nothing pending
        response = _verify(api_client)
        assert response.status_code == 400 and response.json()["code"] == "otp_expired"
        assert OtpRequest.objects.get().expires_at <= timezone.now()

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (ProviderUnavailable("down"), 503, "otp_unavailable"),
            (ProviderRejected("max", status=429, code=60202), 429, "otp_attempts_exceeded"),
            (ProviderRejected("bad", status=400), 400, "otp_invalid"),
        ],
    )
    def test_provider_errors(self, api_client, monkeypatch, error, status, code):
        OtpRequestFactory(phone_e164="+919876543210")

        class Broken:
            def check(self, phone, code):
                raise error

        monkeypatch.setattr(twilio_verify, "client", lambda: Broken())
        response = _verify(api_client)
        assert response.status_code == status and response.json()["code"] == code

    @pytest.mark.parametrize("payload", [{}, {"phone": "9876543210"}, {"phone": "9876543210", "code": "12ab56"}, {"phone": "9876543210", "code": "12345678901"}])
    def test_validation(self, api_client, payload):
        response = api_client.post(VERIFY, payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"

    def test_throttled_per_ip(self, api_client, settings):
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp_ip": "1/10min"}}
        assert _verify(api_client).status_code == 400
        assert _verify(api_client).status_code == 429

    def test_throttled_per_phone_across_addresses(self, api_client, settings):
        """PLAN §3.3: ``otp/verify`` is throttled ``otp`` (per phone) too — guesses spread over many addresses stop."""
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp": "2/10min"}}
        statuses = [_verify(api_client, phone, code="111111", REMOTE_ADDR=f"203.0.113.{n}").status_code for n, phone in enumerate(("9876543210", "+91 98765 43210", "09876543210"))]
        assert statuses == [400, 400, 429]
        assert _verify(api_client, "9876543211", REMOTE_ADDR="203.0.113.50").status_code == 400  # another number has its own budget
        assert _send(api_client, REMOTE_ADDR="203.0.113.51").status_code == 200  # checks do not use up the budget for sending codes


class TestToken:
    def test_errors(self):
        good = signing.dumps({"p": "+919876543210", "u": "LEAD", "t": timezone.now().isoformat()}, salt=otp.TOKEN_SALT, compress=True)
        cases = [
            ("", "+919876543210", "verification_required"),
            ("garbage", "+919876543210", "verification_invalid"),
            (good, "+919876543211", "verification_mismatch"),
            (signing.dumps({"p": "+919876543210", "u": "LEAD", "t": timezone.now().isoformat()}, salt="other"), "+919876543210", "verification_invalid"),
        ]
        for token, phone, code in cases:
            with pytest.raises(DomainError) as caught:
                otp.read_token(token, phone_e164=phone)
            assert caught.value.code == code
        with pytest.raises(DomainError) as caught:
            otp.read_token(good, phone_e164="+919876543210", purpose="APPROVAL")
        assert caught.value.code == "verification_mismatch"

    def test_expiry(self, settings):
        settings.LEADS_VERIFICATION_TOKEN_TTL_SECONDS = 60
        token = signing.dumps({"p": "+919876543210", "u": "LEAD", "t": timezone.now().isoformat()}, salt=otp.TOKEN_SALT, compress=True)
        with freeze_time(timezone.now() + dt.timedelta(seconds=61)), pytest.raises(DomainError) as caught:
            otp.read_token(token, phone_e164="+919876543210")
        assert caught.value.code == "verification_expired"


def test_fake_backend_rejects_unknown_numbers():
    client = twilio_verify.FakeVerifyClient()
    with pytest.raises(VerificationNotFound):
        client.check("+919876543210", "000000")
    client.send("+919876543210")
    assert client.check("+919876543210", "123456").approved is False
    assert client.check("+919876543210", "000000").approved is True
    with pytest.raises(VerificationNotFound):
        client.check("+919876543210", "000000")  # a code is used once


@override_settings(LEADS_OTP_BACKEND="carrier-pigeon")
def test_unknown_backend_is_unavailable(api_client):
    response = _send(api_client)
    assert response.status_code == 503 and response.json()["code"] == "otp_unavailable"
