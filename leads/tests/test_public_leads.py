"""POST /api/public/v1/leads/ — verification token, kinds/forms, payload rules, honeypot, customer link, events."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from customers.tests.factories import CustomerFactory
from leads.models import Lead, LeadEvent
from leads.services.intake import resolve_kind_and_form

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/leads/"


def _post(client, body, **extra):
    return client.post(URL, body, format="json", **extra)


class TestSubmit:
    def test_verified_lead_shape_and_side_effects(self, api_client, verified):
        token = verified("9876543210")
        body = {
            "form": "home_booking",
            "name": "  Asha  ",
            "phone": "98765 43210",
            "email": " Asha@Example.COM ",
            "pincode": "682016",
            "page": "/",
            "details": {"Pincode": "682016", "Empty": "", "Nothing": None, "Bill": 3500},
            "calculator": {"inputs": {"bill": 3500}, "outputs": {"kw": 3}},
            "utm": {"utm_source": "google", "gclid": "abc"},
            "verification_token": token,
        }
        response = _post(api_client, body)
        assert response.status_code == 201, response.json()
        receipt = response.json()
        assert set(receipt) == {"uid", "number", "kind", "form", "status", "created_at", "message"}
        assert (receipt["kind"], receipt["form"], receipt["status"]) == ("HOME_ENQUIRY", "HOME_BOOKING", "NEW") and receipt["number"].startswith("L-")
        lead = Lead.objects.get()
        assert (lead.name, lead.phone_e164, lead.email, lead.source_url, lead.ip) == ("Asha", "+919876543210", "asha@example.com", "/", "127.0.0.1")
        assert lead.payload == {"details": {"Pincode": "682016", "Bill": 3500}, "calculator": {"inputs": {"bill": 3500}, "outputs": {"kw": 3}}, "utm": {"utm_source": "google", "gclid": "abc"}}
        assert lead.otp_verified_at is not None and lead.assignee is None and lead.customer is None
        assert LeadEvent.objects.get(lead=lead).event == "CREATED"
        assert AuditLog.objects.get(action="leads.lead_created").actor_kind == "CUSTOMER"
        event = OutboxEvent.objects.get(event_type="leads.created")
        assert event.payload["lead_uid"] == str(lead.uid) and event.payload["channel"] == "website"

    def test_phone_without_verification_is_refused(self, api_client):
        response = _post(api_client, {"kind": "HOME_ENQUIRY", "name": "Asha", "phone": "9876543210"})
        assert response.status_code == 400 and response.json()["code"] == "verification_required" and "verification_token" in response.json()["errors"]
        assert not Lead.objects.exists()

    def test_token_for_another_number_is_refused(self, api_client, verified):
        token = verified("9876543210")
        response = _post(api_client, {"kind": "HOME_ENQUIRY", "name": "Asha", "phone": "9876543211", "verification_token": token})
        assert response.status_code == 400 and response.json()["code"] == "verification_mismatch"

    def test_contact_by_email_needs_no_phone_and_no_otp(self, api_client):
        response = _post(api_client, {"kind": "CONTACT", "name": "Asha", "email": "asha@example.com", "message": "Call me"})
        assert response.status_code == 201 and response.json()["form"] == "CONTACT_PAGE"
        assert Lead.objects.get().otp_verified_at is None

    def test_phone_required_for_other_kinds(self, api_client):
        response = _post(api_client, {"kind": "GROUP_PURCHASE", "name": "Asha", "email": "a@example.com"})
        assert response.status_code == 400 and "phone" in response.json()["errors"]
        assert _post(api_client, {"kind": "CONTACT", "name": "Asha"}).status_code == 400

    def test_existing_customer_is_linked_and_its_owner_assigned(self, api_client, verified, make_user):
        owner = make_user()
        customer = CustomerFactory(phone_e164="+919876543210", owner=owner)
        response = _post(api_client, {"form": "footer", "name": "Asha", "phone": "9876543210", "verification_token": verified()})
        lead = Lead.objects.get(uid=response.json()["uid"])
        assert (lead.customer, lead.assignee, lead.status) == (customer, owner, "NEW")

    def test_repeat_enquiries_are_kept(self, api_client, verified):
        token = verified()
        for _ in range(2):
            assert _post(api_client, {"form": "footer", "name": "Asha", "phone": "9876543210", "verification_token": token}).status_code == 201
        assert Lead.objects.count() == 2 and len(set(Lead.objects.values_list("number", flat=True))) == 2

    def test_idempotency_key(self, api_client):
        body = {"kind": "CONTACT", "name": "Asha", "email": "asha@example.com"}
        first = _post(api_client, body, HTTP_IDEMPOTENCY_KEY="lead-submit-0001")
        second = _post(api_client, body, HTTP_IDEMPOTENCY_KEY="lead-submit-0001")
        assert first.json() == second.json() and Lead.objects.count() == 1
        reused = _post(api_client, {**body, "name": "Other"}, HTTP_IDEMPOTENCY_KEY="lead-submit-0001")
        assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"

    def test_throttled_as_public_write(self, api_client, settings):
        from leads.views.public import LeadSubmitView

        assert LeadSubmitView().get_throttle_scope(type("R", (), {"method": "POST"})()) == "public_write" and LeadSubmitView.authentication_classes == []
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_write": "1/min"}}
        body = {"kind": "CONTACT", "name": "Asha", "email": "asha@example.com"}
        assert [_post(api_client, body).status_code for _ in range(2)] == [201, 429]


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"kind": "CONTACT", "name": "  ", "email": "a@example.com"}, "name"),
            ({"kind": "CONTACT", "name": "A" * 256, "email": "a@example.com"}, "name"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "form": "bogus"}, "form"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "form": "studio"}, "form"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "details": "x"}, "details"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "details": {"a": {"b": 1}}}, "details"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "details": {f"k{i}": i for i in range(31)}}, "details"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "calculator": [1, 2]}, "calculator"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "calculator": {"a": "x" * 9000}}, "calculator"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "calculator": {"a": {"b": {"c": {"d": {"e": 1}}}}}}, "calculator"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "utm": {"evil": "x"}}, "utm"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "page": "/" + "p" * 500}, "page"),
            ({"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "pincode": "68201"}, "pincode"),
            ({"kind": "CONTACT", "name": "Asha", "email": "not-an-email"}, "email"),
            ({"kind": "CONTACT", "name": "Asha", "phone": "12345"}, "phone"),
            ({"kind": "HOME_ENQUIRY", "form": "contact_page", "name": "Asha", "phone": "9876543210"}, "form"),
        ],
    )
    def test_field_errors(self, api_client, body, field):
        response = _post(api_client, body)
        assert response.status_code == 400 and field in response.json()["errors"], response.json()

    def test_honeypot(self, api_client):
        response = _post(api_client, {"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "website": "http://spam"})
        assert response.status_code == 400 and response.json()["errors"] == {"non_field_errors": ["Invalid submission."]}

    def test_details_values_are_trimmed_like_before(self, api_client):
        _post(api_client, {"kind": "CONTACT", "name": "Asha", "email": "a@example.com", "details": {"Note": "x" * 2100, "K" * 70: 1}})
        details = Lead.objects.get().payload["details"]
        assert len(details["Note"]) == 2000 and "K" * 64 in details


def test_kind_and_form_complete_each_other():
    assert resolve_kind_and_form(None, None) == ("CONTACT", "OTHER")
    assert resolve_kind_and_form(None, "QUOTE_REQUEST") == ("ADVANCED_CALC", "QUOTE_REQUEST")
    assert resolve_kind_and_form("REFERRAL", None) == ("REFERRAL", "REFERRAL_PARTNER")
    assert resolve_kind_and_form("GROUP_PURCHASE", "OTHER") == ("GROUP_PURCHASE", "OTHER")


class TestServiceGuards:
    """``intake.submit_lead`` is also the entry point of the legacy adapter (``require_verification=False``)."""

    def test_legacy_adapter_path_keeps_the_lead_unverified(self):
        from leads.services import intake

        lead = intake.submit_lead(data={"form": "FOOTER", "name": "Asha", "phone_e164": "+919876543210"}, ip="203.0.113.1", require_verification=False)
        assert (lead.kind, lead.otp_verified_at, str(lead.ip)) == ("HOME_ENQUIRY", None, "203.0.113.1")

    @pytest.mark.parametrize("data", [{"kind": "HOME_ENQUIRY", "name": "Asha"}, {"kind": "CONTACT", "name": "Asha"}])
    def test_contact_requirements_hold_without_the_serializer(self, data):
        from core.errors import DomainError
        from leads.services import intake

        with pytest.raises(DomainError) as caught:
            intake.submit_lead(data=data, require_verification=False)
        assert caught.value.code == "validation_error" and "phone" in caught.value.errors
