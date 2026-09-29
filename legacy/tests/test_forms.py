"""Old website forms through the shim, replaying the submissions recorded against a private legacy server
(``leads/tests/fixtures/legacy_backend/forms.json``, ``careers/tests/legacy/applications_cases.json``): the legacy
status and body (volatile id/timestamps aside), the rows stored by the platform services, and every refusal in the
legacy error shapes. Approved differences are those of docs/decisions/leads-customers.md and careers-reference.md."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from careers.models import JobApplication
from careers.tests.legacy import sample_files
from careers.tests.test_public_parity import load as load_careers
from careers.tests.test_public_parity import seed as seed_careers
from core.errors import DomainError
from leads.models import AffiliateApplication, Lead, WarrantyRequest
from leads.services import twilio_verify
from leads.tests.conftest import load_fixture
from legacy.services import forms as form_service
from legacy.services.ids import SHIM_ID_OFFSET
from legacy.tests.conftest import ordered, throttled

pytestmark = pytest.mark.django_db
FORMS = load_fixture("forms.json")
PATHS = {
    "lead_collection_home": "/legacy/api/lead-collection-home/",
    "affiliate_applications": "/legacy/api/affiliate-applications/",
    "warranty_service_requests": "/legacy/api/warranty-service-requests/",
}
APPROVED = {("lead_collection_home", "leading_zero_phone")}
VOLATILE = {"id", "created_at", "updated_at"}


def _stable(body):
    if isinstance(body, dict):
        return {key: ("<volatile>" if key in VOLATILE else _stable(value)) for key, value in body.items()}
    return body


def _cases(form):
    return [pytest.param(name, case, id=name) for name, case in FORMS[form].items() if (form, name) not in APPROVED]


@pytest.mark.parametrize("form", list(PATHS))
def test_forms_answer_as_the_legacy_endpoints(api_client, form):
    for name, case in FORMS[form].items():
        if (form, name) in APPROVED:
            continue
        response = api_client.post(PATHS[form], case["request"], format="json")
        assert response.status_code == case["status"], (name, response.json())
        body = ordered(response)
        assert _stable(body) == _stable(case["body"]), name
        assert list(body) == list(case["body"]), name


def test_accepted_forms_are_stored_by_the_platform_services(api_client):
    api_client.post(PATHS["lead_collection_home"], FORMS["lead_collection_home"]["contact_page"]["request"], format="json")
    lead = Lead.objects.get()
    assert (lead.phone_e164, lead.form, lead.source_url, lead.otp_verified_at) == ("+919876500013", "CONTACT_PAGE", "/contact-us", None)
    body = api_client.post(PATHS["affiliate_applications"], FORMS["affiliate_applications"]["valid"]["request"], format="json").json()
    assert AffiliateApplication.objects.get().email == "partner.one@example.com" and body["data"]["id"] >= SHIM_ID_OFFSET
    api_client.post(PATHS["warranty_service_requests"], FORMS["warranty_service_requests"]["valid"]["request"], format="json")
    assert WarrantyRequest.objects.get().description == "Inverter shows error E02"


def test_leading_zero_phone_is_the_approved_difference(api_client):
    response = api_client.post(PATHS["lead_collection_home"], FORMS["lead_collection_home"]["leading_zero_phone"]["request"], format="json")
    assert FORMS["lead_collection_home"]["leading_zero_phone"]["status"] == 400 and response.status_code == 201


def test_forms_throttle_is_public_write(api_client, settings):
    throttled(settings, "public_write", "2/min")
    assert [api_client.post(PATHS["warranty_service_requests"], {}, format="json").status_code for _ in range(3)] == [400, 400, 429]


# ── OTP ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("form,name", [("send_otp", "empty"), ("send_otp", "phone_too_long"), ("verify_otp", "empty"), ("verify_otp", "code_too_long")])
def test_otp_validation_is_the_legacy_serializer_errors(api_client, form, name):
    case = FORMS[form][name]
    response = api_client.post(f"/legacy/api/{form.replace('_', '-')}/", case["request"], format="json")
    assert (response.status_code, ordered(response)) == (case["status"], case["body"])


def test_otp_send_and_verify_record_the_quote_enquiry(api_client):
    sent = api_client.post("/legacy/api/send-otp/", {"phone_number": "9876500041", "name": "Otp Lead"}, format="json")
    assert sent.status_code == 200 and sent.json() == {"status": "pending"}
    wrong = api_client.post("/legacy/api/verify-otp/", {"phone_number": "9876500041", "code": "999999", "name": "Otp Lead"}, format="json")
    assert wrong.status_code == 400 and wrong.json() == {"error": "The code is not correct."}
    verified = api_client.post("/legacy/api/verify-otp/", {"phone_number": "9876500041", "code": twilio_verify.FAKE_APPROVED_CODE, "name": "Otp Lead"}, format="json")
    lead = Lead.objects.get()
    assert verified.status_code == 200 and verified.json() == {
        "status": "approved",
        "message": "OTP verified successfully. Your quote request has been recorded.",
        "quote_id": f"QUOTE_{lead.uid.hex[:8].upper()}",
    }
    assert (lead.name, lead.form, lead.source_url, lead.phone_e164) == ("Otp Lead", "QUOTE_REQUEST", "/advanced-calculator", "+919876500041") and lead.otp_verified_at


def test_otp_refuses_foreign_numbers_before_the_provider(api_client):
    sent_before = len(twilio_verify.SENT)
    response = api_client.post("/legacy/api/send-otp/", {"phone_number": "+15005550006"}, format="json")
    assert response.status_code == 400 and response.json() == {"error": "Enter a valid 10-digit Indian mobile number."}
    assert len(twilio_verify.SENT) == sent_before


@pytest.mark.parametrize("phone", ["+14155550100", "+447911123456", "+971501234567"])
def test_otp_never_texts_a_valid_foreign_mobile(api_client, phone):
    """Toll-fraud guard (leads-customers decision 2): a VALID foreign mobile is refused before the provider, exactly
    like the canonical ``otp/send`` (``regions={"IN"}``) — not only numbers the numbering plan rejects."""
    sent_before = len(twilio_verify.SENT)
    response = api_client.post("/legacy/api/send-otp/", {"phone_number": phone}, format="json")
    assert response.status_code == 400 and response.json() == {"error": "Enter a valid 10-digit Indian mobile number."}
    assert len(twilio_verify.SENT) == sent_before
    response = api_client.post("/legacy/api/verify-otp/", {"phone_number": phone, "code": "123456"}, format="json")
    assert response.status_code == 400 and not Lead.all_objects.filter(phone_e164=phone).exists()


def test_otp_throttles_per_legacy_phone_field(api_client, settings):
    throttled(settings, "otp", "2/10min")
    statuses = [api_client.post("/legacy/api/send-otp/", {"phone_number": "9876500042"}, format="json").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert api_client.post("/legacy/api/send-otp/", {"phone_number": "9876500043"}, format="json").status_code == 200


@pytest.mark.parametrize("path", ["/legacy/api/send-otp/", "/legacy/api/verify-otp/"])
@pytest.mark.parametrize("phone", ["12345", "+14155550100"])
def test_otp_phone_throttle_falls_back_to_the_client_ip_instead_of_failing_open(api_client, settings, caplog, path, phone):
    """A phone the canonical throttle cannot normalise (junk, a foreign number) is budgeted per client IP in the
    ``otp`` scope, like the canonical endpoints — the throttle must neither crash nor fail open."""
    throttled(settings, "otp", "2/10min")
    statuses = [api_client.post(path, {"phone_number": phone, "code": "123456"}, format="json").status_code for _ in range(3)]
    assert statuses == [400, 400, 429]
    assert "throttle cache unavailable" not in caplog.text


# ── job applications ──────────────────────────────────────────────────────────────────────────────────────────────
APPLICATIONS = load_careers("shared")
CASES = __import__("json").loads((__import__("pathlib").Path(__file__).resolve().parents[2] / "careers/tests/legacy/applications_cases.json").read_text())["cases"]
APPLICATION_APPROVED = {"fake_pdf_content", "closed_posting"}


def _multipart(case):
    form = dict(case["form"])
    for field, (name, kind) in case["files"].items():
        form[field] = SimpleUploadedFile(name, sample_files.build(kind))
    return form


@pytest.mark.parametrize("case", [case for case in CASES if case["name"] not in APPLICATION_APPROVED], ids=lambda case: case["name"])
def test_job_applications_answer_as_the_legacy_endpoint(api_client, case, tmp_path, settings):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    seed_careers(APPLICATIONS)
    response = api_client.post("/legacy/api/job-applications/", _multipart(case), format="multipart")
    body = ordered(response)
    assert response.status_code == case["status"], (case["name"], body)
    if case["status"] == 201:
        assert body["message"] == form_service.APPLICATION_MESSAGE and body["status"] == "success"
        stored = {key: value for key, value in case["stored"].items() if key != "status"}
        assert {key: body["data"][key] for key in stored} == stored
        assert JobApplication.objects.get().name == case["stored"]["full_name"]
    else:
        assert body["message"] == "Validation failed" and set(body["errors"]) == set(case["errors"]), body


def test_job_applications_answer_the_legacy_integer_position_id(api_client, tmp_path, settings):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    seed_careers(APPLICATIONS)
    case = next(case for case in CASES if case["name"] == "valid_posting")
    body = api_client.post("/legacy/api/job-applications/", _multipart(case), format="multipart").json()
    assert body["data"]["position_id"] == case["stored"]["position_id"] and body["data"]["resume"] is None


def test_unknown_position_id_is_a_validation_error():
    with pytest.raises(DomainError) as caught:
        form_service.submit_application(_querydict({"position_id": "424242"}), ip=None)
    assert caught.value.errors == {"position_id": ['Invalid pk "424242" - object does not exist.']}


def _querydict(values):
    from django.http import QueryDict

    data = QueryDict(mutable=True)
    data.update(values)
    return data


def test_lead_form_still_requires_the_phone_where_the_canonical_form_accepts_an_email(api_client):
    response = api_client.post(PATHS["lead_collection_home"], {"name": "Asha", "phone_number": "", "email": "a@example.com"}, format="json")
    assert response.status_code == 400 and response.json()["errors"] == {"phone_number": ["This field may not be blank."]}
    assert not Lead.objects.exists()
