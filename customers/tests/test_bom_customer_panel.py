"""The customer contract the BOM calculator's Customer panel depends on.

The panel lets a sales person type a customer while pricing a system, so that a
quotation can be raised from the same screen. Three behaviours it relies on are
assumptions until something asserts them.
"""

import pytest

from customers.tests.factories import CustomerFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/customers/"


@pytest.fixture
def head(make_user):
    return make_user(grants={"customers": "*"}, scopes={"customers": "all"})


@pytest.fixture
def client(auth_client, head):
    return auth_client(head)


class TestBomCustomerPanel:
    def test_name_and_phone_alone_create_a_customer(self, client):
        """The panel marks a draft quotable on these two fields; nothing else is required."""
        response = client.post(URL, {"name": "Asha Menon", "phone": "98450 12345"}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["name"] == "Asha Menon"
        assert body["uid"] and body["code"]

    def test_a_repeat_number_names_the_customer_it_belongs_to(self, client):
        """`phone_taken` carries `existing_customer`, which is what lets the panel
        offer that customer instead of reporting a failure."""
        existing = CustomerFactory(phone_e164="+919845012345")
        response = client.post(URL, {"name": "Someone Else", "phone": "98450 12345"}, format="json")
        assert response.status_code == 409, response.json()
        body = response.json()
        assert body["code"] == "phone_taken"
        assert body["errors"]["existing_customer"] == [str(existing.uid)]

    def test_that_customer_can_then_be_loaded_by_uid(self, client):
        """The panel follows the uid with a GET, so the redirect has to resolve."""
        existing = CustomerFactory(phone_e164="+919845012345", name="Asha Menon")
        conflict = client.post(URL, {"name": "Dup", "phone": "98450 12345"}, format="json")
        uid = conflict.json()["errors"]["existing_customer"][0]
        fetched = client.get(f"{URL}{uid}/")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == existing.name

    def test_out_of_scope_customers_are_not_named(self, auth_client, make_user):
        """An executive scoped to their own rows is told the number is taken but not
        whose it is — so the panel correctly offers nothing rather than a customer
        they cannot see."""
        CustomerFactory(phone_e164="+919845012345")
        scoped = auth_client(make_user(grants={"customers": ["view", "create"]}, scopes={"customers": "owned"}))
        response = scoped.post(URL, {"name": "Mine", "phone": "98450 12345"}, format="json")
        assert response.status_code == 409
        assert "existing_customer" not in response.json()["errors"]

    def test_search_finds_a_customer_by_name_phone_and_code(self, client):
        """The three things a sales person has to hand on a call."""
        customer = CustomerFactory(name="Asha Menon", phone_e164="+919845012345")
        for term in ("Asha", "9845012345", customer.code):
            found = client.get(f"{URL}?search={term}")
            assert found.status_code == 200, term
            assert str(customer.uid) in [row["uid"] for row in found.json()["results"]], term

    def test_creating_needs_the_create_verb_not_edit(self, auth_client, make_user):
        """The panel hides Save on `customers.create`; this is the other half."""
        viewer = auth_client(make_user(grants={"customers": ["view", "edit"]}, scopes={"customers": "all"}))
        response = viewer.post(URL, {"name": "Asha", "phone": "98450 12345"}, format="json")
        assert response.status_code == 403

    def test_a_bad_pincode_is_refused_by_field(self, client):
        """The panel renders field errors verbatim, so they have to be per-field."""
        response = client.post(URL, {"name": "Asha", "phone": "98450 12345", "pincode": "12"}, format="json")
        assert response.status_code == 400
        assert "pincode" in response.json()["errors"]
