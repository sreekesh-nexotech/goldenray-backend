"""Staff leads/affiliate-applications/ and leads/warranty-requests/ — permissions, scope, edit, transitions, assign."""

import pytest

from audit.models import AuditLog
from customers.tests.factories import CustomerFactory
from leads.models import AffiliateApplication, WarrantyRequest
from leads.tests.factories import AffiliateApplicationFactory, WarrantyRequestFactory

pytestmark = pytest.mark.django_db
AFFILIATE = "/api/v1/leads/affiliate-applications/"
WARRANTY = "/api/v1/leads/warranty-requests/"
CASES = [(AFFILIATE, AffiliateApplicationFactory), (WARRANTY, WarrantyRequestFactory)]


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"leads": "*", "customers": ["view"]}, scopes={"leads": "all", "customers": "all"}))


@pytest.mark.parametrize(("url", "factory"), CASES)
class TestBoth:
    def test_anonymous_and_permissions(self, api_client, auth_client, make_user, url, factory):
        row = factory()
        assert api_client.get(url).status_code == 401
        viewer = auth_client(make_user(grants={"leads": ["view"]}, scopes={"leads": "all"}))
        assert viewer.get(url).status_code == 200 and viewer.get(f"{url}{row.uid}/").status_code == 200
        assert viewer.patch(f"{url}{row.uid}/", {"full_name": "x"}, format="json").status_code == 403
        assert viewer.post(f"{url}{row.uid}/transition/", {"status": "REJECTED"}, format="json").status_code == 403
        assert viewer.post(f"{url}{row.uid}/assign/", {"assignee_uid": None}, format="json").status_code == 403
        assert viewer.delete(f"{url}{row.uid}/").status_code == 403
        assert viewer.post(url, {}, format="json").status_code == 403  # no create action: created only by the website form (default deny)

    def test_owned_scope(self, auth_client, make_user, url, factory):
        seller = make_user(grants={"leads": ["view"]}, scopes={"leads": "owned"})
        mine, other = factory(assignee=seller), factory()
        client = auth_client(seller)
        assert [row["uid"] for row in client.get(url).json()["results"]] == [str(mine.uid)]
        assert client.get(f"{url}{other.uid}/").status_code == 404

    def test_edit_assign_delete_and_stale(self, client, make_user, url, factory, django_assert_max_num_queries):
        row = factory()
        factory.create_batch(3, assignee=make_user())
        with django_assert_max_num_queries(8):
            assert client.get(url).json()["count"] == 4
        response = client.patch(f"{url}{row.uid}/", {"full_name": "Renamed", "phone": "+91 99999 11111", "expected_version": 1}, format="json")
        assert response.status_code == 200 and (response.json()["full_name"], response.json()["phone"], response.json()["version"]) == ("Renamed", "+919999911111", 2)
        assert client.patch(f"{url}{row.uid}/", {"full_name": "Again", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert client.patch(f"{url}{row.uid}/", {"phone": "12"}, format="json").status_code == 400
        user = make_user()
        assert client.post(f"{url}{row.uid}/assign/", {"assignee_uid": str(user.uid)}, format="json").json()["assignee"]["uid"] == str(user.uid)
        assert client.post(f"{url}{row.uid}/assign/", {"assignee_uid": str(user.uid), "expected_version": 1}, format="json").json()["code"] == "stale_version"
        stale = client.post(f"{url}{row.uid}/transition/", {"status": "REJECTED", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        invalid = client.post(f"{url}{row.uid}/transition/", {"status": "BOGUS"}, format="json")
        assert invalid.status_code == 400 and invalid.json()["code"] == "validation_error" and "status" in invalid.json()["errors"]
        assert client.delete(f"{url}{row.uid}/").status_code == 204
        assert client.get(f"{url}{row.uid}/").status_code == 404
        assert AuditLog.objects.filter(action__startswith="leads.", action__endswith="_deleted").count() == 1


class TestAffiliate:
    def test_transitions(self, client):
        row = AffiliateApplicationFactory()
        url = f"{AFFILIATE}{row.uid}/transition/"
        assert client.post(url, {"status": "CONTACTED", "note": "called"}, format="json").json()["status"] == "CONTACTED"
        blocked = client.post(url, {"status": "NEW"}, format="json")
        assert blocked.status_code == 409 and blocked.json()["code"] == "invalid_transition" and "status" in blocked.json()["errors"]
        assert client.post(url, {"status": "REJECTED"}, format="json").json()["status"] == "REJECTED"
        assert client.post(url, {"status": "NEW"}, format="json").json()["status"] == "NEW"
        assert client.post(url, {"status": "APPROVED"}, format="json").json()["status"] == "APPROVED"
        assert client.post(url, {"status": "REJECTED"}, format="json").json()["code"] == "invalid_transition"
        assert client.post(url, {"status": "BOGUS"}, format="json").status_code == 400
        assert AuditLog.objects.filter(action="leads.affiliate_application_status_changed").first().after["note"] in ("called", "")

    def test_labels_are_accepted_on_edit(self, client):
        row = AffiliateApplicationFactory()
        body = client.patch(f"{AFFILIATE}{row.uid}/", {"profession": "Financial Advisor", "district": "KOLLAM"}, format="json").json()
        assert (body["profession"], body["district"]) == ("FINANCIAL_ADVISOR", "Kollam")
        assert client.get(AFFILIATE, {"profession": "FINANCIAL_ADVISOR"}).json()["count"] == 1


class TestWarranty:
    def test_transitions(self, client):
        row = WarrantyRequestFactory()
        url = f"{WARRANTY}{row.uid}/transition/"
        for status in ("IN_PROGRESS", "RESOLVED", "IN_PROGRESS", "RESOLVED", "CLOSED"):
            assert client.post(url, {"status": status}, format="json").json()["status"] == status
        assert client.post(url, {"status": "NEW"}, format="json").json()["code"] == "invalid_transition"
        assert WarrantyRequest.objects.get().version == 6

    def test_link_a_visible_customer(self, auth_client, make_user, client):
        row = WarrantyRequestFactory()
        customer = CustomerFactory()
        body = client.patch(f"{WARRANTY}{row.uid}/", {"customer_uid": str(customer.uid), "system_details": {"kw": 5}}, format="json").json()
        assert body["customer"]["uid"] == str(customer.uid) and body["system_details"] == {"kw": 5}
        assert client.patch(f"{WARRANTY}{row.uid}/", {"customer_uid": None}, format="json").json()["customer"] is None
        blind = auth_client(make_user(grants={"leads": "*"}, scopes={"leads": "all"}))  # no customers.view
        response = blind.patch(f"{WARRANTY}{row.uid}/", {"customer_uid": str(customer.uid)}, format="json")
        assert response.status_code == 404 and "customer_uid" in response.json()["errors"]
        assert client.patch(f"{WARRANTY}{row.uid}/", {"system_details": "x"}, format="json").status_code == 400

    def test_filters(self, client):
        WarrantyRequestFactory(status=WarrantyRequest.Status.RESOLVED)
        WarrantyRequestFactory(issue_type="PANEL_DAMAGE")
        assert client.get(WARRANTY, {"status": "RESOLVED"}).json()["count"] == 1
        assert client.get(WARRANTY, {"issue_type": "PANEL_DAMAGE"}).json()["count"] == 1
        assert client.get(WARRANTY, {"unassigned": "true"}).json()["count"] == 2


def test_affiliate_statuses_cover_the_model():
    from leads.services.workflows import TRANSITIONS

    assert set(TRANSITIONS[AffiliateApplication]) == set(AffiliateApplication.Status.values)
    assert set(TRANSITIONS[WarrantyRequest]) == set(WarrantyRequest.Status.values)
