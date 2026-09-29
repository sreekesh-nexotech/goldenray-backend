"""Staff leads/installations/ — CRUD, district default, showcase photo rules, assignee rule, scope, stale version."""

import pytest

from leads.models import CustomerInstallation
from leads.tests.factories import CustomerInstallationFactory
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/leads/installations/"
NEW = {"customer_name": "Rajesh", "phone": "9876543210", "pincode": "688008", "address": "Vadakkal", "capacity_kw": "5.5", "installed_on": "2026-02-01", "system_type": "ON_GRID"}


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"leads": "*"}, scopes={"leads": "all"}))


def test_anonymous_and_permissions(api_client, auth_client, make_user):
    row = CustomerInstallationFactory()
    assert api_client.get(URL).status_code == 401
    viewer = auth_client(make_user(grants={"leads": ["view"]}, scopes={"leads": "all"}))
    assert viewer.get(f"{URL}{row.uid}/").status_code == 200
    assert viewer.post(URL, NEW, format="json").status_code == 403
    assert viewer.patch(f"{URL}{row.uid}/", {"is_showcase": True}, format="json").status_code == 403
    assert viewer.delete(f"{URL}{row.uid}/").status_code == 403


def test_create_takes_the_district_from_the_pincode_list(client, legacy_pincodes):
    response = client.post(URL, NEW, format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["district"], body["phone"], body["capacity_kw"], body["status"], body["is_showcase"]) == ("ALAPPUZHA", "+919876543210", "5.500", "COMPLETED", False)
    moved = client.patch(f"{URL}{body['uid']}/", {"pincode": "682016"}, format="json").json()
    assert moved["district"] == "ERNAKULAM"


def test_showcase_photo_must_be_a_public_image(client):
    row = CustomerInstallationFactory()
    private = MediaAssetFactory(visibility="PRIVATE", cdn_url="", kind="DOCUMENT")
    assert client.patch(f"{URL}{row.uid}/", {"photo_uid": str(private.uid)}, format="json").status_code == 400
    photo = MediaAssetFactory()
    body = client.patch(f"{URL}{row.uid}/", {"photo_uid": str(photo.uid), "is_showcase": True}, format="json").json()
    assert body["photo"]["uid"] == str(photo.uid) and body["is_showcase"] is True
    from media import usage

    assert [(ref.label, ref.live) for ref in usage.references(photo)] == [("leads.customerinstallation.photo", 1)]


@pytest.mark.parametrize(("field", "value"), [("pincode", "68800"), ("capacity_kw", "0"), ("installed_on", "yesterday"), ("status", "DONE"), ("phone", "12")])
def test_validation(client, field, value):
    response = client.post(URL, {**NEW, field: value}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]


def test_assignee_rule_and_scope(auth_client, make_user):
    editor = make_user(grants={"leads": ["view", "create", "edit"]}, scopes={"leads": "owned"})
    other = make_user()
    client = auth_client(editor)
    assert client.post(URL, {**NEW, "assignee_uid": str(other.uid)}, format="json").json()["code"] == "assign_forbidden"
    mine = client.post(URL, {**NEW, "assignee_uid": str(editor.uid)}, format="json")
    assert mine.status_code == 201
    CustomerInstallationFactory()
    assert [row["uid"] for row in client.get(URL).json()["results"]] == [mine.json()["uid"]]
    assert client.patch(f"{URL}{mine.json()['uid']}/", {"assignee_uid": str(other.uid)}, format="json").json()["code"] == "assign_forbidden"


def test_new_installation_is_assigned_to_its_creator(auth_client, make_user):
    """As for leads: a Sales Executive (owned scope) keeps seeing what they entered; leaving it to nobody needs manage."""
    editor = make_user(grants={"leads": ["view", "create", "edit"]}, scopes={"leads": "owned"})
    client = auth_client(editor)
    created = client.post(URL, NEW, format="json")
    assert created.status_code == 201 and created.json()["assignee"]["uid"] == str(editor.uid)
    assert [row["uid"] for row in client.get(URL).json()["results"]] == [created.json()["uid"]]
    refused = client.post(URL, {**NEW, "assignee_uid": None}, format="json")
    assert refused.status_code == 403 and refused.json()["code"] == "assign_forbidden"
    manager = auth_client(make_user(grants={"leads": "*"}, scopes={"leads": "all"}))
    assert manager.post(URL, {**NEW, "assignee_uid": None}, format="json").json()["assignee"] is None


def test_list_filters_queries_stale_and_delete(client, django_assert_max_num_queries):
    CustomerInstallationFactory.create_batch(3, photo=MediaAssetFactory())
    row = CustomerInstallationFactory(status=CustomerInstallation.Status.PLANNED, pincode="695001", district="THIRUVANANTHAPURAM")
    with django_assert_max_num_queries(8):
        assert client.get(URL).json()["count"] == 4
    assert client.get(URL, {"status": "PLANNED"}).json()["count"] == 1
    assert client.get(URL, {"district": "thiruvananthapuram"}).json()["count"] == 1
    assert client.get(URL, {"installed_from": "2025-01-10", "installed_to": "2025-01-10"}).json()["count"] == 4
    assert client.patch(f"{URL}{row.uid}/", {"status": "COMPLETED", "expected_version": 5}, format="json").json()["code"] == "stale_version"
    assert client.delete(f"{URL}{row.uid}/").status_code == 204
    assert CustomerInstallation.all_objects.get(pk=row.pk).deleted_at is not None
