"""procurement/suppliers/ — CRUD, code uniqueness, contact validation, in-use guard."""

import pytest

from audit.models import AuditLog
from procurement.models import Supplier
from procurement.tests.factories import BatchFactory, SupplierFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/procurement/suppliers/"


def detail(supplier):
    return f"{URL}{supplier.uid}/"


def test_permissions(api_client, outsider, viewer):
    supplier = SupplierFactory()
    assert api_client.get(URL).status_code == 401
    assert outsider.get(URL).status_code == 403
    assert viewer.get(URL).status_code == 200
    assert viewer.post(URL, {"code": "S1", "name": "x"}, format="json").status_code == 403
    assert viewer.patch(detail(supplier), {"name": "y"}, format="json").status_code == 403
    assert viewer.delete(detail(supplier)).status_code == 403


def test_crud(client, procurement_user):
    response = client.post(URL, {"code": "SUP001", "name": " Master Supplier Co. ", "gstin": "32aabcu9603r1zm", "contact": {"name": "Asha", "phone": "0484 000000"}}, format="json")
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["name"] == "Master Supplier Co." and body["gstin"] == "32AABCU9603R1ZM" and body["contact"]["name"] == "Asha"
    supplier = Supplier.objects.get()
    taken = client.post(URL, {"code": "sup001", "name": "Other"}, format="json")
    assert taken.status_code == 409 and taken.json()["code"] == "supplier_code_taken"
    updated = client.patch(detail(supplier), {"is_active": False, "expected_version": 1}, format="json")
    assert updated.status_code == 200 and updated.json()["is_active"] is False
    stale = client.patch(detail(supplier), {"name": "x", "expected_version": 1}, format="json")
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert AuditLog.objects.filter(action="procurement.supplier_created", actor=procurement_user).exists()
    assert client.get(URL, {"is_active": "false"}).json()["count"] == 1


@pytest.mark.parametrize("payload,field", [({"code": "bad code!", "name": "x"}, "code"), ({"code": "S2", "name": "x", "gstin": "123"}, "gstin"), ({"code": "S3", "name": ""}, "name")])
def test_validation(client, payload, field):
    response = client.post(URL, payload, format="json")
    assert response.status_code == 400 and field in response.json()["errors"]


def test_contact_must_be_short_strings(client):
    response = client.post(URL, {"code": "S4", "name": "x", "contact": {"note": "y" * 300}}, format="json")
    assert response.status_code == 400


def test_delete_refused_while_batches_exist(client):
    supplier = SupplierFactory()
    batch = BatchFactory(supplier=supplier)
    refused = client.delete(detail(supplier))
    assert refused.status_code == 409 and refused.json()["code"] == "supplier_in_use"
    batch.soft_delete()
    assert client.delete(detail(supplier)).status_code == 204


def test_list_query_budget(client, django_assert_max_num_queries):
    SupplierFactory.create_batch(15)
    with django_assert_max_num_queries(10):
        assert client.get(URL, {"page_size": 50}).json()["count"] == 15
