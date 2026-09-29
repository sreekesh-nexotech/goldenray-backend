"""Customer location approval: staff request, the customer surface (token + OTP), the paper fallback."""

import datetime as dt

import pytest
from django.utils import timezone

from core.errors import DomainError
from leads.services import twilio_verify
from media.tests import files
from site_inspections.models import Inspection, LocationApproval
from site_inspections.models.choices import Status
from site_inspections.services import approvals, lifecycle
from site_inspections.tests.factories import InspectionFactory, NamedBytes, completed_fields
from site_inspections.views.customer import ApprovalRespondView, ApprovalSendOtpView, ApprovalSummaryView

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"
CUSTOMER = "/api/customer/v1/inspection-approvals/"
CODE = twilio_verify.FAKE_APPROVED_CODE


@pytest.fixture
def completed(engineer):
    inspection = InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS)
    completed_fields(inspection, engineer)
    return lifecycle.submit(inspection, user=engineer)


@pytest.fixture
def link(auth_client, engineer, completed):
    response = auth_client(engineer).post(f"{BASE}{completed.uid}/approval/request/", {}, format="json")
    assert response.status_code == 201, response.json()
    return response.json()["link"].rstrip("/").rsplit("/", 1)[-1]


class TestRequest:
    def test_request_stores_snapshot_and_moves_to_pending(self, auth_client, engineer, completed):
        response = auth_client(engineer).post(f"{BASE}{completed.uid}/approval/request/", {}, format="json")
        body = response.json()
        assert body["approval"]["status"] == "PENDING" and set(body["approval"]["location_snapshot"]) == {"PANEL_AREA", "EQUIPMENT_AREA"}
        token = body["link"].rstrip("/").rsplit("/", 1)[-1]
        approval = LocationApproval.objects.get(inspection=completed)
        assert approval.token_hash == approvals.hash_token(token) and token not in str(approval.__dict__)
        assert Inspection.objects.get(pk=completed.pk).status == Status.CUSTOMER_APPROVAL_PENDING

    def test_a_new_link_supersedes_the_pending_one(self, head, completed):
        approvals.request_approval(completed, user=head)
        approvals.request_approval(Inspection.objects.get(pk=completed.pk), user=head, customer_phone_e164="+919812345678")
        assert list(LocationApproval.objects.filter(inspection=completed).order_by("number").values_list("status", flat=True)) == ["SUPERSEDED", "PENDING"]

    def test_preconditions(self, auth_client, engineer, head, make_user, completed):
        client = auth_client(engineer)
        draft = InspectionFactory(engineer=engineer)
        assert client.post(f"{BASE}{draft.uid}/approval/request/", {}, format="json").json()["code"] == "invalid_status"
        Inspection.objects.filter(pk=completed.pk).update(site_suitability="NOT_SUITABLE")
        assert client.post(f"{BASE}{completed.uid}/approval/request/", {}, format="json").json()["code"] == "site_not_suitable"
        Inspection.objects.filter(pk=completed.pk).update(site_suitability="CONDITIONAL")
        viewer = make_user(grants={"site_inspections": ["view"]}, scopes={"site_inspections": "all"})
        assert auth_client(viewer).post(f"{BASE}{completed.uid}/approval/request/", {}, format="json").json()["code"] == "submit_or_approve_required"
        completed.customer.__class__.objects.filter(pk=completed.customer_id).update(phone_e164="")
        assert auth_client(head).post(f"{BASE}{completed.uid}/approval/request/", {}, format="json").json()["code"] == "phone_required"

    def test_list_approvals(self, auth_client, head, link, completed):
        assert auth_client(head).get(f"{BASE}{completed.uid}/approvals/").json()["results"][0]["otp_verified"] is False


class TestCustomerSurface:
    def test_summary_shape_and_private_caching(self, api_client, link, completed):
        response = api_client.get(f"{CUSTOMER}{link}/")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"inspection_number", "approval_number", "status", "customer_name", "phone_masked", "expires_at", "visit_date", "site", "system", "locations", "responded_at"}
        assert body["status"] == "PENDING" and len(body["locations"]) == 2 and body["locations"][0]["photo_url"]
        assert "remarks" not in str(body) and "engineer" not in body
        assert "no-store" in response["Cache-Control"] and "private" in response["Cache-Control"]

    def test_throttle_scope_and_no_session(self):
        for view in (ApprovalSummaryView, ApprovalSendOtpView, ApprovalRespondView):
            assert view.throttle_scope == "customer" and view.authentication_classes == []

    def test_unknown_token(self, api_client):
        response = api_client.get(f"{CUSTOMER}not-a-token/")
        assert response.status_code == 404 and response.json()["code"] == "approval_not_found"

    def test_otp_and_approve(self, api_client, link, completed):
        sent = api_client.post(f"{CUSTOMER}{link}/send-otp/")
        assert sent.status_code == 200 and sent.json()["phone_masked"].startswith("+91")
        assert twilio_verify.SENT[-1]["to"] == completed.customer.phone_e164
        wrong = api_client.post(f"{CUSTOMER}{link}/respond/", {"code": "123456", "decision": "APPROVED"}, format="json")
        assert wrong.status_code == 400 and wrong.json()["code"] == "otp_invalid"
        ok = api_client.post(f"{CUSTOMER}{link}/respond/", {"code": CODE, "decision": "APPROVED"}, format="json", REMOTE_ADDR="10.1.2.3")
        assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
        approval = LocationApproval.objects.get(inspection=completed)
        assert approval.otp_verified_at is not None and approval.responded_ip == "10.1.2.3"
        assert Inspection.objects.get(pk=completed.pk).status == Status.APPROVED
        again = api_client.post(f"{CUSTOMER}{link}/send-otp/")
        assert again.status_code == 409 and again.json()["code"] == "approval_closed"

    def test_reject_needs_comment_and_signature_is_stored(self, api_client, link, completed):
        api_client.post(f"{CUSTOMER}{link}/send-otp/")
        response = api_client.post(f"{CUSTOMER}{link}/respond/", {"code": CODE, "decision": "REJECTED"}, format="json")
        assert response.json()["code"] == "comment_required"
        signature = NamedBytes(files.png(), "sign.png")
        response = api_client.post(f"{CUSTOMER}{link}/respond/", {"code": CODE, "decision": "REJECTED", "comment": "Move it left", "signature": signature}, format="multipart")
        assert response.status_code == 200, response.json()
        approval = LocationApproval.objects.get(inspection=completed)
        assert approval.signature_asset.kind == "SIGNATURE" and approval.customer_comment == "Move it left"
        assert Inspection.objects.get(pk=completed.pk).status == Status.REJECTED

    def test_expired_link(self, api_client, link, completed):
        LocationApproval.objects.filter(inspection=completed).update(expires_at=timezone.now() - dt.timedelta(minutes=1))
        response = api_client.post(f"{CUSTOMER}{link}/send-otp/")
        assert response.status_code == 410 and response.json()["code"] == "link_expired"

    def test_superseded_link_cannot_answer(self, api_client, head, link, completed):
        approvals.request_approval(Inspection.objects.get(pk=completed.pk), user=head)
        assert api_client.post(f"{CUSTOMER}{link}/send-otp/").json()["code"] == "approval_closed"

    def test_on_hold_inspection_cannot_be_answered(self, api_client, head, link, completed):
        api_client.post(f"{CUSTOMER}{link}/send-otp/")
        lifecycle.hold(Inspection.objects.get(pk=completed.pk), user=head, reason="wait")
        assert api_client.post(f"{CUSTOMER}{link}/respond/", {"code": CODE, "decision": "APPROVED"}, format="json").json()["code"] == "approval_closed"

    def test_validation(self, api_client, link):
        response = api_client.post(f"{CUSTOMER}{link}/respond/", {"code": "x", "decision": "MAYBE"}, format="json")
        assert response.status_code == 400 and {"code", "decision"} <= set(response.json()["errors"])


class TestPaperFallback:
    def test_paper_approval_needs_scan_and_reason(self, auth_client, head, engineer, link, completed):
        client = auth_client(head)
        url = f"{BASE}{completed.uid}/approval/paper/"
        assert client.post(url, {"scan": NamedBytes(files.pdf(), "scan.pdf"), "reason": " "}, format="multipart").json()["code"] == "validation_error"
        with pytest.raises(DomainError) as error:
            approvals.record_paper_approval(completed, user=head, scan=NamedBytes(files.pdf(), "scan.pdf"), reason=" ")
        assert error.value.code == "reason_required"
        response = client.post(url, {"scan": NamedBytes(files.pdf(), "scan.pdf"), "reason": "Customer has no smartphone"}, format="multipart")
        assert response.status_code == 200, response.json()
        assert response.json()["approved_by_staff"]["uid"] == str(head.uid) and response.json()["status"] == "APPROVED"
        assert Inspection.objects.get(pk=completed.pk).status == Status.APPROVED
        assert auth_client(engineer).post(url, {"scan": NamedBytes(files.pdf(), "scan.pdf"), "reason": "x"}, format="multipart").status_code == 403
        again = client.post(url, {"scan": NamedBytes(files.jpeg(), "scan.jpg"), "reason": "again"}, format="multipart")
        assert again.status_code == 409 and again.json()["code"] == "invalid_status"


class TestIndianMobileOnly:
    """The one-time code goes by SMS, so a link is never issued for a number that cannot receive it — a foreign or a
    landline number (final review); the paper fallback stays available."""

    @pytest.mark.parametrize("phone", ["+447911123456", "+914842000000"])
    def test_head_cannot_choose_a_number_that_cannot_receive_the_code(self, auth_client, head, completed, phone):
        response = auth_client(head).post(f"{BASE}{completed.uid}/approval/request/", {"customer_phone": phone}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "phone_not_indian_mobile"
        assert not LocationApproval.objects.filter(inspection=completed).exists()

    def test_customer_record_with_a_foreign_number_is_refused(self, auth_client, engineer, completed):
        completed.customer.__class__.objects.filter(pk=completed.customer_id).update(phone_e164="+447911123456", alt_phone="")
        response = auth_client(engineer).post(f"{BASE}{completed.uid}/approval/request/", {}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "phone_not_indian_mobile"

    def test_send_otp_refuses_a_foreign_number_stored_on_an_older_link(self, api_client, link, completed):
        LocationApproval.objects.filter(inspection=completed).update(customer_phone_e164="+447911123456")
        response = api_client.post(f"{CUSTOMER}{link}/send-otp/")
        assert response.status_code == 400 and response.json()["code"] == "otp_phone_unsupported"
        assert twilio_verify.SENT == []
