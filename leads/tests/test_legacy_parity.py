"""Parity with the legacy main backend's public forms (captured by ``fixtures/capture_legacy.py`` from a private copy of
the UAT legacy server; ``fixtures/legacy_backend/forms.json``).

For every captured request the legacy payload is renamed onto the canonical endpoint (the legacy shim's job:
``phone_number`` → ``phone``, ``source`` → ``form``; everything else keeps its name) and:

* a legacy validation error (400) is a 400 on the canonical endpoint naming the same field(s);
* a legacy success (201) is a 201 whose stored row reads back exactly as the legacy response did (name, 10-digit
  phone, source, page, details / e-mail, profession, district / issue, description);
* the differences are deliberate and listed in ``APPROVED_DIFFERENCES`` (also in docs/decisions/leads-customers.md).
"""

import pytest

from customers.services.phones import national_digits
from leads.models import AffiliateApplication, Lead, WarrantyRequest
from leads.services import twilio_verify
from leads.services.twilio_verify import ProviderUnavailable
from leads.tests.conftest import load_fixture

pytestmark = pytest.mark.django_db
FORMS = load_fixture("forms.json")
FIELD_MAP = {"phone_number": "phone", "source": "form"}
APPROVED_DIFFERENCES = {
    # A leading trunk zero is a valid spelling of an Indian mobile number; the legacy regex refused it.
    ("lead_collection_home", "leading_zero_phone"): "accepted (201)",
    # Legacy leaked Twilio/proxy exception text with a 400; the platform answers 503 otp_unavailable.
    ("send_otp", "fake_twilio"): "503 otp_unavailable",
    ("verify_otp", "fake_twilio"): "400 otp_expired (nothing was sent)",
    # Legacy blocked a number for 30 days after any request (even a failed send); the platform throttles instead.
    ("send_otp", "repeat_within_30_days"): "throttles (otp 5/10min per phone, otp_ip per IP) + daily cap",
    # Legacy sent SMS to any country; the platform sends only to Indian mobile numbers.
    ("send_otp", "foreign_number"): "400 phone",
}


def _canonical(payload: dict) -> dict:
    return {FIELD_MAP.get(key, key): value for key, value in payload.items()}


def _cases(form: str, status: int):
    return [pytest.param(name, case, id=name) for name, case in FORMS[form].items() if case["status"] == status and (form, name) not in APPROVED_DIFFERENCES]


@pytest.mark.parametrize(("name", "case"), _cases("lead_collection_home", 400))
def test_lead_validation_errors_match(api_client, name, case):
    response = api_client.post("/api/public/v1/leads/", _canonical(case["request"]), format="json")
    assert response.status_code == 400, response.json()
    expected = {FIELD_MAP.get(field, field) for field in case["body"]["errors"]}
    assert expected <= set(response.json()["errors"]), (expected, response.json())


@pytest.mark.parametrize(("name", "case"), _cases("lead_collection_home", 201))
def test_accepted_leads_store_what_legacy_stored(api_client, verified, name, case):
    payload = _canonical(case["request"])
    payload["verification_token"] = verified(payload["phone"])
    response = api_client.post("/api/public/v1/leads/", payload, format="json")
    assert response.status_code == 201, response.json()
    lead = Lead.objects.get(uid=response.json()["uid"])
    legacy = case["body"]
    assert lead.name == legacy["name"]
    assert national_digits(lead.phone_e164) == legacy["phone_number"]
    assert lead.form.lower() == legacy["source"]
    assert lead.source_url == legacy["page"]
    assert lead.payload.get("details", {}) == legacy["details"]


def test_leading_zero_phone_is_an_approved_difference(api_client, verified):
    case = FORMS["lead_collection_home"]["leading_zero_phone"]
    assert case["status"] == 400
    payload = {**_canonical(case["request"]), "verification_token": verified("9876500018")}
    assert api_client.post("/api/public/v1/leads/", payload, format="json").status_code == 201


@pytest.mark.parametrize(
    ("form", "url", "model"),
    [("affiliate_applications", "/api/public/v1/affiliate-applications/", AffiliateApplication), ("warranty_service_requests", "/api/public/v1/warranty-requests/", WarrantyRequest)],
)
def test_form_validation_and_storage_match(api_client, form, url, model):
    for name, case in FORMS[form].items():
        response = api_client.post(url, case["request"], format="json")
        if case["status"] == 400:
            assert response.status_code == 400, (name, response.json())
            assert set(case["body"]["errors"]) <= set(response.json()["errors"]), (name, response.json())
            continue
        assert response.status_code == 201, (name, response.json())
        row = model.objects.get(uid=response.json()["uid"])
        legacy = case["body"]["data"]
        assert (row.full_name, national_digits(row.phone_e164)) == (legacy["full_name"], legacy["phone"]), name
        if model is AffiliateApplication:
            assert (row.email, row.get_profession_display(), row.district) == (legacy["email"], legacy["profession"], legacy["district"])
        else:
            assert (row.get_issue_type_display(), row.description) == (legacy["issue_type"], legacy["description"])


def test_legacy_form_throttle_is_replaced_by_public_write(api_client, settings):
    assert FORMS["warranty_throttle_statuses_same_ip"] == [400, 400, 400, 400, 400, 429]  # legacy: 5/min per X-Forwarded-For
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_write": "5/min"}}
    statuses = [api_client.post("/api/public/v1/warranty-requests/", {}, format="json").status_code for _ in range(6)]
    assert statuses == [400, 400, 400, 400, 400, 429]


@pytest.mark.parametrize(("name", "case"), _cases("send_otp", 400))
def test_otp_send_validation_matches(api_client, name, case):
    response = api_client.post("/api/public/v1/otp/send/", _canonical(case["request"]), format="json")
    assert response.status_code == 400
    assert {FIELD_MAP.get(field, field) for field in case["body"]} <= set(response.json()["errors"])


@pytest.mark.parametrize(("name", "case"), _cases("verify_otp", 400))
def test_otp_verify_validation_matches(api_client, name, case):
    response = api_client.post("/api/public/v1/otp/verify/", _canonical(case["request"]), format="json")
    assert response.status_code == 400
    assert {FIELD_MAP.get(field, field) for field in case["body"]} <= set(response.json()["errors"])


def test_otp_approved_differences(api_client, monkeypatch):
    legacy = FORMS["send_otp"]
    assert legacy["fake_twilio"]["status"] == 400 and "verify.twilio.com" in legacy["fake_twilio"]["body"]["error"]  # the leak
    assert legacy["repeat_within_30_days"]["status"] == 429 and legacy["repeat_within_30_days"]["body"]["days_remaining"] == 30
    assert api_client.post("/api/public/v1/otp/send/", _canonical(legacy["foreign_number"]["request"]), format="json").status_code == 400

    class Down:
        def send(self, phone):
            raise ProviderUnavailable("proxy said no")

    monkeypatch.setattr(twilio_verify, "client", lambda: Down())
    response = api_client.post("/api/public/v1/otp/send/", _canonical(legacy["fake_twilio"]["request"]), format="json")
    assert response.status_code == 503 and response.json()["code"] == "otp_unavailable" and "proxy" not in response.content.decode()
    monkeypatch.undo()
    verify = api_client.post("/api/public/v1/otp/verify/", _canonical(FORMS["verify_otp"]["fake_twilio"]["request"]), format="json")
    assert verify.status_code == 400 and verify.json()["code"] == "otp_expired"
