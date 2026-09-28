"""The thin Twilio Verify client: requests shape, error mapping, configuration (integration → env, fail closed)."""

import pytest
import requests
import responses

from company.tests.factories import IntegrationFactory
from leads.services import twilio_verify
from leads.services.twilio_verify import ProviderRejected, ProviderUnavailable, TwilioConfig, TwilioVerifyClient, VerificationNotFound

SID = "AC" + "a" * 32
SERVICE = "VA" + "b" * 32
TOKEN = "t" * 32
BASE = f"https://verify.twilio.com/v2/Services/{SERVICE}"


@pytest.fixture
def client():
    return TwilioVerifyClient(TwilioConfig(SID, TOKEN, SERVICE), timeout=3)


@responses.activate
def test_send_posts_the_number_with_basic_auth(client):
    responses.post(f"{BASE}/Verifications", json={"sid": "VE123", "status": "pending"}, status=201)
    result = client.send("+919876543210")
    assert (result.sid, result.status) == ("VE123", "pending")
    call = responses.calls[0]
    assert call.request.body == "To=%2B919876543210&Channel=sms"
    assert call.request.headers["Authorization"].startswith("Basic ")


@responses.activate
def test_check_approved_and_pending(client):
    responses.post(f"{BASE}/VerificationCheck", json={"sid": "VE1", "status": "approved", "valid": True})
    responses.post(f"{BASE}/VerificationCheck", json={"sid": "VE1", "status": "pending", "valid": False})
    assert client.check("+919876543210", "123456").approved is True
    assert client.check("+919876543210", "654321").approved is False
    assert "Code=654321" in responses.calls[1].request.body


@responses.activate
@pytest.mark.parametrize(
    ("resource", "status", "body", "error"),
    [
        ("VerificationCheck", 404, {"code": 20404, "message": "not found"}, VerificationNotFound),
        ("Verifications", 429, {"code": 60203, "message": "Max send attempts reached"}, ProviderRejected),
        ("Verifications", 400, {"code": 60200, "message": "Invalid parameter"}, ProviderRejected),
        ("Verifications", 503, {}, ProviderUnavailable),
        # Our credentials or service SID are wrong (rotated token, deleted Verify service): a configuration fault,
        # never the visitor's number or code.
        ("Verifications", 401, "not json", ProviderUnavailable),
        ("VerificationCheck", 401, {"code": 20003, "message": "Authenticate"}, ProviderUnavailable),
        ("Verifications", 404, {"code": 20404, "message": "The requested resource was not found"}, ProviderUnavailable),
    ],
)
def test_errors_are_mapped(client, resource, status, body, error):
    if isinstance(body, dict):
        responses.post(f"{BASE}/{resource}", json=body, status=status)
    else:
        responses.post(f"{BASE}/{resource}", body=body, status=status)
    with pytest.raises(error) as caught:
        client.check("+919876543210", "1") if resource == "VerificationCheck" else client.send("+919876543210")
    if isinstance(caught.value, ProviderRejected):
        assert caught.value.status == status and caught.value.code == (body.get("code") if isinstance(body, dict) else None)


@responses.activate
def test_network_failure_is_unavailable(client):
    responses.post(f"{BASE}/Verifications", body=requests.ConnectionError("proxy said no"))
    with pytest.raises(ProviderUnavailable) as caught:
        client.send("+919876543210")
    assert "ConnectionError" in str(caught.value) and "proxy" not in str(caught.value)


def test_config_repr_hides_the_token():
    assert TOKEN not in repr(TwilioConfig(SID, TOKEN, SERVICE))


@pytest.mark.django_db
def test_config_prefers_the_enabled_integration(settings):
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_VERIFY_SERVICE_SID = "ACenv", "env-token", "VAenv"
    assert twilio_verify.load_config() == TwilioConfig("ACenv", "env-token", "VAenv")
    IntegrationFactory(key="TWILIO", config={"account_sid": SID, "verify_service_sid": SERVICE, "auth_token": TOKEN})
    assert twilio_verify.load_config() == TwilioConfig(SID, TOKEN, SERVICE)


@pytest.mark.django_db
def test_missing_configuration_fails_closed(settings):
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_VERIFY_SERVICE_SID = "", "", ""
    with pytest.raises(ProviderUnavailable) as caught:
        twilio_verify.load_config()
    assert "account_sid" in str(caught.value)


@pytest.mark.django_db
@responses.activate
def test_twilio_backend_end_to_end(api_client, settings):
    settings.LEADS_OTP_BACKEND = "twilio"
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_VERIFY_SERVICE_SID = SID, TOKEN, SERVICE
    responses.post(f"{BASE}/Verifications", json={"sid": "VE9", "status": "pending"}, status=201)
    responses.post(f"{BASE}/VerificationCheck", json={"sid": "VE9", "status": "approved", "valid": True})
    assert api_client.post("/api/public/v1/otp/send/", {"phone": "9876543210"}, format="json").status_code == 200
    response = api_client.post("/api/public/v1/otp/verify/", {"phone": "9876543210", "code": "482913"}, format="json")
    assert response.status_code == 200 and response.json()["verification_token"]
    assert twilio_verify.SENT == []  # the fake backend was not used


@pytest.mark.django_db
@responses.activate
def test_rejected_credentials_are_an_outage_not_a_wrong_code(api_client, settings, caplog):
    """A rotated auth token (Twilio 401) must not tell a visitor that their correct code is wrong, nor that their
    number cannot receive SMS: it is 503 ``otp_unavailable`` and an ERROR in the log for the operators."""
    settings.LEADS_OTP_BACKEND = "twilio"
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN, settings.TWILIO_VERIFY_SERVICE_SID = SID, TOKEN, SERVICE
    responses.post(f"{BASE}/Verifications", json={"sid": "VE9", "status": "pending"}, status=201)
    responses.post(f"{BASE}/VerificationCheck", json={"code": 20003, "message": "Authenticate"}, status=401)
    assert api_client.post("/api/public/v1/otp/send/", {"phone": "9876543210"}, format="json").status_code == 200
    with caplog.at_level("ERROR", logger="flarize.leads.otp"):
        response = api_client.post("/api/public/v1/otp/verify/", {"phone": "9876543210", "code": "482913"}, format="json")
    assert response.status_code == 503 and response.json()["code"] == "otp_unavailable"
    assert any(record.levelname == "ERROR" and "401" in record.getMessage() for record in caplog.records)
    assert TOKEN not in caplog.text

    responses.replace(responses.POST, f"{BASE}/Verifications", json={"code": 20404, "message": "not found"}, status=404)
    sent = api_client.post("/api/public/v1/otp/send/", {"phone": "9876543211"}, format="json")
    assert sent.status_code == 503 and sent.json()["code"] == "otp_unavailable"
