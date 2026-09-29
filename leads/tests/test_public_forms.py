"""POST affiliate-applications/ and warranty-requests/ (public): shape, codes or legacy labels, customer link, idempotency."""

import pytest

from audit.models import AuditLog
from customers.tests.factories import CustomerFactory
from leads.models import AffiliateApplication, WarrantyRequest

pytestmark = pytest.mark.django_db
AFFILIATE = "/api/public/v1/affiliate-applications/"
WARRANTY = "/api/public/v1/warranty-requests/"


def test_affiliate_application_shape(api_client):
    body = {"full_name": "Partner One", "phone": "9876500021", "email": "P1@Example.com", "profession": "BUILDER_DEVELOPER", "district": "ernakulam", "website": ""}
    response = api_client.post(AFFILIATE, body, format="json")
    assert response.status_code == 201
    receipt = response.json()
    assert set(receipt) == {"uid", "full_name", "profession", "district", "status", "created_at", "message"} and receipt["message"] == "Message sent!"
    row = AffiliateApplication.objects.get()
    assert (row.phone_e164, row.email, row.profession, row.district, row.status) == ("+919876500021", "p1@example.com", "BUILDER_DEVELOPER", "Ernakulam", "NEW")
    assert AuditLog.objects.get(action="leads.affiliate_application_received").actor_kind == "CUSTOMER"
    assert "9876500021" not in response.content.decode()  # the receipt never echoes the phone


def test_warranty_request_links_the_customer_and_keeps_system_details(api_client):
    customer = CustomerFactory(phone_e164="+919876500031")
    body = {"full_name": "Owner", "phone": "+91 98765 00031", "issue_type": "KSEB / Net Metering", "description": "  meter  ", "system_details": {"capacity_kw": 5, "inverter": "Growatt"}}
    response = api_client.post(WARRANTY, body, format="json")
    assert response.status_code == 201 and response.json()["issue_type"] == "NET_METERING"
    row = WarrantyRequest.objects.get()
    assert (row.customer, row.description, row.system_details) == (customer, "meter", {"capacity_kw": 5, "inverter": "Growatt"})


@pytest.mark.parametrize("details", ["text", {"a": {"b": {"c": {"d": 1}}}}, {"blob": "x" * 5000}])
def test_system_details_is_validated(api_client, details):
    response = api_client.post(WARRANTY, {"full_name": "O", "phone": "9876500031", "issue_type": "OTHER", "system_details": details}, format="json")
    assert response.status_code == 400 and "system_details" in response.json()["errors"]


def test_forms_are_idempotent_and_public_write(api_client):
    from leads.views.public import AffiliateSubmitView, WarrantySubmitView

    for view in (AffiliateSubmitView, WarrantySubmitView):
        assert view.authentication_classes == [] and view().get_throttle_scope(type("R", (), {"method": "POST"})()) == "public_write"
    body = {"full_name": "O", "phone": "9876500031", "issue_type": "OTHER"}
    first = api_client.post(WARRANTY, body, format="json", HTTP_IDEMPOTENCY_KEY="warranty-0000001")
    second = api_client.post(WARRANTY, body, format="json", HTTP_IDEMPOTENCY_KEY="warranty-0000001")
    assert first.json() == second.json() and WarrantyRequest.objects.count() == 1
