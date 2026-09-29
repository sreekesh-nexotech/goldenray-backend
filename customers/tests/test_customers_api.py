"""Staff /api/v1/customers/ — permissions, owned scope, CRUD, phone uniqueness, owner rules, delete guard, notes."""

import pytest

from audit.models import AuditLog
from customers.models import Customer, CustomerNote
from customers.services import merge
from customers.tests.factories import CustomerFactory, CustomerNoteFactory
from leads.tests.factories import LeadFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/customers/"
NEW = {"name": "Priya Nair", "phone": "98450 12345", "email": "Priya@Example.com", "pincode": "695001", "current_bill": "3000.00", "bill_cycle": "BIMONTHLY"}


def detail(customer, suffix=""):
    return f"{URL}{customer.uid}/{suffix}"


@pytest.fixture
def head(make_user):
    return make_user(grants={"customers": "*", "leads": "*"}, scopes={"customers": "all", "leads": "all"})


@pytest.fixture
def client(auth_client, head):
    return auth_client(head)


@pytest.fixture
def executive(make_user):
    return make_user(grants={"customers": ["view", "create", "edit"]}, scopes={"customers": "owned"})


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        customer = CustomerFactory()
        for response in (api_client.get(URL), api_client.get(detail(customer, "timeline/")), api_client.post(detail(customer, "merge/")), api_client.get(detail(customer, "notes/"))):
            assert response.status_code == 401

    def test_missing_permissions_are_403(self, auth_client, make_user):
        customer = CustomerFactory()
        viewer = auth_client(make_user(grants={"customers": ["view"]}, scopes={"customers": "all"}))
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(customer, "timeline/")).status_code == 200
        assert viewer.post(URL, NEW, format="json").status_code == 403
        assert viewer.patch(detail(customer), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(customer)).status_code == 403
        assert viewer.post(detail(customer, "merge/"), {"into_uid": str(CustomerFactory().uid)}, format="json").status_code == 403
        assert viewer.post(detail(customer, "notes/"), {"body": "x"}, format="json").status_code == 403
        assert auth_client(make_user(grants={"leads": "*"})).get(URL).status_code == 403

    def test_owned_scope(self, auth_client, executive):
        mine = CustomerFactory(owner=executive)
        other = CustomerFactory()
        CustomerNoteFactory(customer=other)
        client = auth_client(executive)
        assert [row["uid"] for row in client.get(URL).json()["results"]] == [str(mine.uid)]
        for response in (client.get(detail(other)), client.get(detail(other, "notes/")), client.get(detail(other, "timeline/")), client.post(detail(other, "notes/"), {"body": "x"}, format="json")):
            assert response.status_code == 404


class TestCrud:
    def test_create_normalises_and_owns(self, client, head):
        response = client.post(URL, NEW, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert (body["phone"], body["email"], body["owner"]["uid"], body["source"], body["bill_cycle"]) == ("+919845012345", "priya@example.com", str(head.uid), "SALES_ENTRY", "BIMONTHLY")
        assert body["code"] == "CUST-000001" and client.post(URL, {**NEW, "phone": "9845012346"}, format="json").json()["code"] == "CUST-000002"
        assert AuditLog.objects.filter(action="customers.customer_created").count() == 2

    def test_phone_is_unique_among_live_customers(self, client, auth_client, executive):
        existing = CustomerFactory(phone_e164="+919845012345")
        response = client.post(URL, NEW, format="json")
        assert response.status_code == 409 and response.json()["code"] == "phone_taken" and response.json()["errors"]["existing_customer"] == [str(existing.uid)]
        hidden = auth_client(executive).post(URL, NEW, format="json").json()
        assert hidden["code"] == "phone_taken" and "existing_customer" not in hidden["errors"]  # not disclosed to who cannot see it
        existing.soft_delete()
        assert client.post(URL, NEW, format="json").status_code == 201

    def test_owner_rules(self, auth_client, executive, make_user, client):
        other = make_user()
        own = auth_client(executive)
        assert own.post(URL, {**NEW, "owner_uid": str(other.uid)}, format="json").json()["code"] == "owner_change_forbidden"
        created = own.post(URL, NEW, format="json").json()
        assert created["owner"]["uid"] == str(executive.uid)
        customer = Customer.objects.get(uid=created["uid"])
        assert own.patch(detail(customer), {"owner_uid": str(other.uid)}, format="json").status_code == 403
        assert client.patch(detail(customer), {"owner_uid": str(other.uid)}, format="json").json()["owner"]["uid"] == str(other.uid)
        assert client.post(URL, {**NEW, "phone": "9845012399", "owner_uid": None}, format="json").json()["owner"] is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("name", ""), ("phone", "123"), ("pincode", "12345"), ("email", "nope"), ("latitude", "91"), ("current_bill", "-1"), ("bill_cycle", "WEEKLY"), ("source", "TV"), ("alt_phone", "12")],
    )
    def test_validation_envelope(self, client, field, value):
        response = client.post(URL, {**NEW, field: value}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_update_and_stale_version(self, client):
        customer = CustomerFactory()
        response = client.patch(detail(customer), {"district": "Kochi", "phone": "+91 99999 22222", "expected_version": 1}, format="json")
        assert response.status_code == 200 and (response.json()["district"], response.json()["phone"], response.json()["version"]) == ("Kochi", "+919999922222", 2)
        assert client.patch(detail(customer), {"district": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        taken = CustomerFactory(phone_e164="+919999933333")
        assert client.patch(detail(customer), {"phone": taken.phone_e164}, format="json").json()["code"] == "phone_taken"
        assert client.patch(detail(customer), {}, format="json").json()["version"] == 2

    def test_list_filters_search_and_queries(self, client, head, django_assert_max_num_queries):
        CustomerFactory.create_batch(4, owner=head, lead=LeadFactory())
        CustomerFactory(name="Unique Rajan", source=Customer.Source.WEBSITE, pincode="682016")
        with django_assert_max_num_queries(8):
            body = client.get(URL).json()
        assert body["count"] == 5 and {"uid", "code", "phone", "owner", "lead_uid", "version"} <= set(body["results"][0])
        assert client.get(URL, {"search": "rajan"}).json()["count"] == 1
        assert client.get(URL, {"source": "WEBSITE"}).json()["count"] == 1
        assert client.get(URL, {"filter[unowned]": "true"}).json()["count"] == 1
        assert client.get(URL, {"owner": str(head.uid)}).json()["count"] == 4
        assert client.get(URL, {"pincode": "682016"}).json()["count"] == 1

    def test_delete_is_guarded_by_blocking_dependants(self, client):
        customer = CustomerFactory()
        LeadFactory(customer=customer)  # leads do not block
        merge.register_dependant(CustomerNote, "customer", blocks_delete=True)  # stands in for quotations
        try:
            CustomerNoteFactory(customer=customer)
            response = client.delete(detail(customer))
            assert response.status_code == 409 and response.json()["code"] == "customer_in_use" and "customers.customernote.customer" in response.json()["errors"]
        finally:
            merge.register_dependant(CustomerNote, "customer")
        assert client.delete(detail(customer)).status_code == 204
        assert Customer.all_objects.get(pk=customer.pk).deleted_at is not None and client.get(detail(customer)).status_code == 404


class TestNotes:
    def test_add_list_pin_edit_delete(self, client, head, django_assert_max_num_queries):
        customer = CustomerFactory()
        CustomerNoteFactory(customer=customer, body="older", created_by=head)
        with django_assert_max_num_queries(8):
            assert client.get(detail(customer, "notes/")).json()["count"] == 1
        created = client.post(detail(customer, "notes/"), {"body": "Prefers mornings"}, format="json")
        assert created.status_code == 201 and created.json()["author"]["uid"] == str(head.uid)
        note_url = f"{detail(customer, 'notes/')}{created.json()['uid']}/"
        assert client.patch(note_url, {"pinned": True, "expected_version": 1}, format="json").json()["pinned"] is True
        assert client.patch(note_url, {"body": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        listing = client.get(detail(customer, "notes/")).json()
        assert [row["body"] for row in listing["results"]] == ["Prefers mornings", "older"]
        assert client.post(detail(customer, "notes/"), {"body": ""}, format="json").status_code == 400
        assert client.delete(note_url).status_code == 204
        assert client.get(detail(customer, "notes/")).json()["count"] == 1
        assert AuditLog.objects.filter(action__in=["customers.note_added", "customers.note_updated", "customers.note_deleted"]).count() == 3
