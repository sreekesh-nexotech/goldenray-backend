"""Staff /api/v1/leads/ — permissions, scope, CRUD, workflow (status, lost, spam, assign, convert), notes, events."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from customers.models import Customer
from customers.tests.factories import CustomerFactory
from leads.models import Lead, LeadEvent, LeadNote
from leads.tests.factories import LeadFactory, LeadNoteFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/leads/"
SALES_HEAD = {"leads": "*", "customers": "*"}


def detail(lead, suffix=""):
    return f"{URL}{lead.uid}/{suffix}"


@pytest.fixture
def head(make_user):
    return make_user(grants=SALES_HEAD, scopes={"leads": "all", "customers": "all"})


@pytest.fixture
def client(auth_client, head):
    return auth_client(head)


@pytest.fixture
def executive(make_user):
    return make_user(grants={"leads": ["view", "create", "edit"], "customers": ["view", "create", "edit"]}, scopes={"leads": "owned", "customers": "owned"})


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        lead = LeadFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(detail(lead, "convert/")).status_code == 401
        assert api_client.get(detail(lead, "events/")).status_code == 401

    def test_missing_permissions_are_403(self, auth_client, make_user):
        lead = LeadFactory()
        viewer = auth_client(make_user(grants={"leads": ["view"]}, scopes={"leads": "all"}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(lead, "notes/")).status_code == 200
        assert viewer.post(URL, {"kind": "CONTACT", "name": "x", "email": "x@example.com"}, format="json").status_code == 403
        for suffix, body in [("", {"name": "x"}), ("status/", {"status": "CONTACTED"}), ("mark-lost/", {"reason": "x"}), ("mark-spam/", {}), ("convert/", {}), ("notes/", {"body": "x"})]:
            response = viewer.patch(detail(lead), body, format="json") if not suffix else viewer.post(detail(lead, suffix), body, format="json")
            assert response.status_code == 403, suffix
        assert viewer.delete(detail(lead)).status_code == 403
        assert auth_client(make_user(grants={"leads": ["view", "edit"]}, scopes={"leads": "all"})).post(detail(lead, "assign/"), {"assignee_uid": None}, format="json").status_code == 403
        assert auth_client(make_user(grants={"customers": "*"})).get(URL).status_code == 403

    def test_owned_scope_sees_only_assigned_leads(self, auth_client, executive):
        mine = LeadFactory(assignee=executive)
        other = LeadFactory()
        client = auth_client(executive)
        assert [row["uid"] for row in client.get(URL).json()["results"]] == [str(mine.uid)]
        assert client.get(detail(other)).status_code == 404
        assert client.post(detail(other, "status/"), {"status": "CONTACTED"}, format="json").status_code == 404
        assert client.get(detail(other, "events/")).status_code == 404


class TestCrud:
    def test_list_shape_filters_and_queries(self, client, head, django_assert_max_num_queries):
        customer = CustomerFactory()
        LeadFactory.create_batch(4, assignee=head, customer=customer)
        LeadFactory(status=Lead.Status.LOST, lost_reason="price", kind=Lead.Kind.CONTACT)
        with django_assert_max_num_queries(8):
            body = client.get(URL).json()
        assert body["count"] == 5
        row = body["results"][0]
        assert {"uid", "number", "kind", "form", "status", "phone", "assignee", "customer", "page", "payload", "version"} <= set(row)
        assert client.get(URL, {"status": "LOST"}).json()["count"] == 1
        assert client.get(URL, {"filter[kind]": "CONTACT"}).json()["count"] == 1
        assert client.get(URL, {"assignee": str(head.uid)}).json()["count"] == 4
        assert client.get(URL, {"unassigned": "true"}).json()["count"] == 1
        assert client.get(URL, {"customer": str(customer.uid)}).json()["count"] == 4
        assert client.get(URL, {"search": row["number"]}).json()["count"] >= 1

    def test_create_in_studio(self, client, head):
        existing = CustomerFactory(phone_e164="+919876543210")
        response = client.post(URL, {"kind": "HOME_ENQUIRY", "name": "Walk-in", "phone": "9876543210", "details": {"Visited": "office"}}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert (body["form"], body["status"], body["assignee"]["uid"], body["customer"]["uid"]) == ("STUDIO", "NEW", str(head.uid), str(existing.uid))
        lead = Lead.objects.get()
        assert lead.payload == {"details": {"Visited": "office"}} and lead.otp_verified_at is None and lead.created_by == head
        assert OutboxEvent.objects.get(event_type="leads.created").payload["channel"] == "studio"
        assert LeadEvent.objects.get(lead=lead).by == head

    def test_assigning_someone_else_on_create_needs_manage(self, auth_client, executive, make_user):
        other = make_user()
        response = auth_client(executive).post(URL, {"kind": "CONTACT", "name": "x", "email": "x@example.com", "assignee_uid": str(other.uid)}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "assign_forbidden"

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"kind": "CONTACT", "email": "x@example.com"}, "name"),
            ({"kind": "CONTACT", "name": "x", "phone": "123"}, "phone"),
            ({"kind": "CONTACT", "name": "x"}, "phone"),
            ({"kind": "NOPE", "name": "x", "email": "x@example.com"}, "kind"),
        ],
    )
    def test_validation_envelope(self, client, body, field):
        response = client.post(URL, body, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_update_resets_verification_on_a_new_number(self, client):
        lead = LeadFactory(otp_verified_at="2026-09-01T10:00:00Z")
        response = client.patch(detail(lead), {"phone": "+91 99999 00000", "district": "Ernakulam", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["phone"] == "+919999900000" and response.json()["otp_verified_at"] is None
        assert response.json()["version"] == 2
        assert LeadEvent.objects.get(lead=lead, event="UPDATED").data == {"fields": ["district", "otp_verified_at", "phone_e164"]}

    def test_stale_version(self, client):
        lead = LeadFactory(version=3)
        assert client.patch(detail(lead), {"name": "x", "expected_version": 2}, format="json").json()["code"] == "stale_version"
        for suffix, body in [("status/", {"status": "CONTACTED"}), ("assign/", {"assignee_uid": None}), ("mark-lost/", {"reason": "x"}), ("mark-spam/", {}), ("convert/", {})]:
            response = client.post(detail(lead, suffix), {**body, "expected_version": 2}, format="json")
            assert response.status_code == 409 and response.json()["code"] == "stale_version", suffix

    def test_delete_is_a_soft_delete(self, client):
        lead = LeadFactory()
        assert client.delete(detail(lead)).status_code == 204
        assert not Lead.objects.exists() and Lead.all_objects.get().deleted_at is not None
        assert LeadEvent.objects.filter(lead=lead, event="ARCHIVED").exists()
        assert client.get(detail(lead)).status_code == 404


class TestWorkflow:
    def test_status_moves_and_events(self, client):
        lead = LeadFactory()
        assert client.post(detail(lead, "status/"), {"status": "CONTACTED", "note": "called"}, format="json").json()["status"] == "CONTACTED"
        assert client.post(detail(lead, "status/"), {"status": "QUALIFIED"}, format="json").json()["status"] == "QUALIFIED"
        assert client.post(detail(lead, "status/"), {"status": "QUALIFIED"}, format="json").json()["version"] == 3  # no-op
        events = list(LeadEvent.objects.filter(lead=lead, event="STATUS_CHANGED").order_by("id").values_list("data", flat=True))
        assert events == [{"from": "NEW", "to": "CONTACTED", "note": "called"}, {"from": "CONTACTED", "to": "QUALIFIED"}]
        assert client.post(detail(lead, "status/"), {"status": "LOST"}, format="json").status_code == 400

    def test_lost_needs_a_reason_and_reopens_to_new_only(self, client):
        lead = LeadFactory()
        assert client.post(detail(lead, "mark-lost/"), {}, format="json").status_code == 400
        body = client.post(detail(lead, "mark-lost/"), {"reason": "Too expensive"}, format="json").json()
        assert (body["status"], body["lost_reason"]) == ("LOST", "Too expensive")
        assert client.post(detail(lead, "mark-lost/"), {"reason": "again"}, format="json").json()["code"] == "invalid_transition"
        assert client.post(detail(lead, "status/"), {"status": "CONTACTED"}, format="json").json()["code"] == "invalid_transition"
        reopened = client.post(detail(lead, "status/"), {"status": "NEW"}, format="json").json()
        assert (reopened["status"], reopened["lost_reason"]) == ("NEW", "")

    def test_spam(self, client):
        lead = LeadFactory()
        assert client.post(detail(lead, "mark-spam/"), {"note": "bot"}, format="json").json()["status"] == "SPAM"
        assert client.post(detail(lead, "mark-spam/"), {}, format="json").json()["version"] == 2
        assert client.post(detail(lead, "convert/"), {}, format="json").json()["code"] == "invalid_transition"
        assert client.post(detail(lead, "status/"), {"status": "NEW"}, format="json").json()["status"] == "NEW"

    def test_assign_and_unassign(self, client, make_user):
        lead = LeadFactory()
        other = make_user()
        body = client.post(detail(lead, "assign/"), {"assignee_uid": str(other.uid)}, format="json").json()
        assert body["assignee"]["uid"] == str(other.uid)
        assert client.post(detail(lead, "assign/"), {"assignee_uid": None}, format="json").json()["assignee"] is None
        assert LeadEvent.objects.filter(lead=lead, event="ASSIGNED").count() == 2
        assert client.post(detail(lead, "assign/"), {"assignee_uid": "00000000-0000-0000-0000-000000000000"}, format="json").status_code == 400


class TestConvert:
    def test_creates_a_customer_owned_by_the_assignee(self, client, make_user):
        seller = make_user()
        lead = LeadFactory(assignee=seller, email="a@example.com", pincode="682016", payload={"details": {"Address": "12 Beach Road"}})
        response = client.post(detail(lead, "convert/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["customer_created"] is True and body["lead"]["status"] == "CONVERTED"
        customer = Customer.objects.get(uid=body["customer"]["uid"])
        assert (customer.owner, customer.phone_e164, customer.address, customer.source, customer.lead) == (seller, lead.phone_e164, "12 Beach Road", "WEBSITE", lead)
        assert customer.code.startswith("CUST-")
        event = OutboxEvent.objects.get(event_type="leads.converted")
        assert event.payload == {"lead_uid": str(lead.uid), "customer_uid": str(customer.uid), "customer_created": True}
        assert AuditLog.objects.filter(action="leads.lead_converted").exists()
        again = client.post(detail(lead, "convert/"), {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "lead_already_converted"

    def test_links_the_customer_with_the_same_phone(self, client):
        customer = CustomerFactory(phone_e164="+919812345678")
        lead = LeadFactory(phone_e164="+919812345678")
        body = client.post(detail(lead, "convert/"), {}, format="json").json()
        assert body["customer_created"] is False and body["customer"]["uid"] == str(customer.uid) and Customer.objects.count() == 1

    def test_someone_elses_customer_is_linked_but_not_disclosed(self, auth_client, executive):
        theirs = CustomerFactory(phone_e164="+919812345678", address="Secret street", email="x@example.com")
        lead = LeadFactory(assignee=executive, phone_e164="+919812345678")
        body = auth_client(executive).post(detail(lead, "convert/"), {}, format="json").json()
        assert body["customer"] == {"uid": str(theirs.uid), "code": theirs.code, "name": theirs.name}
        assert auth_client(executive).get(f"/api/v1/customers/{theirs.uid}/").status_code == 404

    def test_chosen_customer_must_be_visible(self, auth_client, executive):
        lead = LeadFactory(assignee=executive, phone_e164="")
        hidden = CustomerFactory()
        client = auth_client(executive)
        assert client.post(detail(lead, "convert/"), {"customer_uid": str(hidden.uid)}, format="json").status_code == 404
        mine = CustomerFactory(owner=executive)
        assert client.post(detail(lead, "convert/"), {"customer_uid": str(mine.uid)}, format="json").json()["customer"]["uid"] == str(mine.uid)

    def test_without_phone_a_customer_must_be_chosen(self, client):
        lead = LeadFactory(phone_e164="", email="a@example.com")
        response = client.post(detail(lead, "convert/"), {}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "phone_required"

    def test_creating_a_customer_needs_customers_create(self, auth_client, make_user):
        user = make_user(grants={"leads": ["view", "edit"]}, scopes={"leads": "all"})
        lead = LeadFactory()
        response = auth_client(user).post(detail(lead, "convert/"), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "customer_create_forbidden"
        CustomerFactory(phone_e164=lead.phone_e164)  # linking an existing one does not
        assert auth_client(user).post(detail(lead, "convert/"), {}, format="json").status_code == 200

    def test_converted_status_is_final(self, client):
        lead = LeadFactory(status=Lead.Status.CONVERTED, customer=CustomerFactory())
        for suffix, body in [("status/", {"status": "NEW"}), ("mark-lost/", {"reason": "x"}), ("mark-spam/", {})]:
            assert client.post(detail(lead, suffix), body, format="json").json()["code"] == "lead_converted"


class TestNotesAndEvents:
    def test_notes(self, client, head, django_assert_max_num_queries):
        lead = LeadFactory()
        LeadNoteFactory.create_batch(3, lead=lead, created_by=head)
        with django_assert_max_num_queries(8):
            assert client.get(detail(lead, "notes/")).json()["count"] == 3
        LeadNote.objects.filter(lead=lead).delete()
        LeadNoteFactory(lead=lead)
        created = client.post(detail(lead, "notes/"), {"body": "Site visit booked"}, format="json")
        assert created.status_code == 201 and created.json()["author"]["uid"] == str(head.uid)
        listing = client.get(detail(lead, "notes/")).json()
        assert listing["count"] == 2 and listing["results"][0]["body"] == "Site visit booked"
        assert client.post(detail(lead, "notes/"), {"body": ""}, format="json").status_code == 400
        assert LeadEvent.objects.filter(lead=lead, event="NOTE_ADDED").exists()

    def test_events_are_cursor_paginated_newest_first(self, client, django_assert_max_num_queries):
        lead = LeadFactory()
        for status in ("CONTACTED", "QUALIFIED", "CONTACTED"):
            client.post(detail(lead, "status/"), {"status": status}, format="json")
        with django_assert_max_num_queries(8):
            assert len(client.get(detail(lead, "events/")).json()["results"]) == 3
        page = client.get(detail(lead, "events/"), {"page_size": 2}).json()
        assert set(page) == {"results", "next", "previous"} and len(page["results"]) == 2 and page["next"]
        assert page["results"][0]["data"]["to"] == "CONTACTED" and page["results"][0]["by"] is not None
        rest = client.get(page["next"]).json()
        assert len(rest["results"]) == 1
