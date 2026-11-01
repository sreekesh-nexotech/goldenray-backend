"""Batch commit / reverse through change requests (DV-155): Procurement asks, an Admin's approval writes the prices."""

from decimal import Decimal

import pytest

from approvals.models import ChangeRequest
from catalog.tests.factories import ComponentFactory
from pricing.models import Price, PriceKind
from procurement.models import Batch, BatchStatus
from procurement.tests.factories import SupplierFactory

pytestmark = pytest.mark.django_db
BATCHES = "/api/v1/procurement/batches/"
CHANGES = "/api/v1/change-requests/"
REQUESTER = {"procurement": ["view", "create", "edit"], "pricing_internal": ["view"], "catalog": ["view"], "change_requests": ["view", "create"]}
APPROVER = {"procurement": "*", "pricing_internal": ["view"], "catalog": "*", "change_requests": "*"}


@pytest.fixture
def requester(auth_client, make_user):
    return auth_client(make_user(grants=REQUESTER, scopes={"change_requests": "owned"}))


@pytest.fixture
def approver(auth_client, make_user):
    return auth_client(make_user(grants=APPROVER, scopes={"change_requests": "all"}))


def drafted(client, *, charges=True) -> Batch:
    """A DRAFT batch the requester authored directly (editing a draft prices nothing)."""
    supplier = SupplierFactory()
    uid = client.post(BATCHES, {"supplier_uid": str(supplier.uid), "invoice_no": "PO-7", "invoice_date": "2026-09-01"}, format="json").json()["uid"]
    a, b = ComponentFactory(sku="a7"), ComponentFactory(sku="b7")
    lines = [{"component_uid": str(a.uid), "qty": "10", "unit_purchase_price": "7000"}, {"component_uid": str(b.uid), "qty": "2", "unit_purchase_price": "5000"}]
    assert client.put(f"{BATCHES}{uid}/lines/", {"lines": lines}, format="json").status_code == 200
    if charges:
        assert client.put(f"{BATCHES}{uid}/charges/", {"charges": [{"kind": "FREIGHT", "amount": "4000"}]}, format="json").status_code == 200
    return Batch.objects.get(uid=uid)


def ask(client, kind, batch, reason="September purchase"):
    return client.post(CHANGES, {"kind": kind, "operation": "UPDATE", "target_uid": str(batch.uid), "payload": {"reason": reason, "expected_version": batch.version}}, format="json")


def test_procurement_cannot_commit_directly(requester):
    batch = drafted(requester)
    assert requester.post(f"{BATCHES}{batch.uid}/commit/", {"reason": "x"}, format="json").status_code == 403
    assert requester.post(f"{BATCHES}{batch.uid}/reverse/", {"reason": "x"}, format="json").status_code == 403


def test_commit_prices_only_on_approval(requester, approver):
    batch = drafted(requester)
    response = ask(requester, "procurement.batch_commit", batch)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["target_label"].startswith(f"Batch {batch.number}")
    assert [line["sku"] for line in body["before"]["lines"]] == ["a7", "b7"] and "landed_unit_cost" in body["before"]["lines"][0]
    assert not Price.objects.exists() and Batch.objects.get(pk=batch.pk).status == BatchStatus.DRAFT
    approved = approver.post(f"{CHANGES}{body['uid']}/approve/", {}, format="json")
    assert approved.status_code == 200, approved.json()
    assert Batch.objects.get(pk=batch.pk).status == BatchStatus.COMMITTED
    assert set(Price.objects.values_list("kind", flat=True)) == {PriceKind.PURCHASE, PriceKind.LANDED}
    assert Price.objects.get(component__sku="a7", kind=PriceKind.PURCHASE).amount == Decimal("7000.00")


def test_a_batch_that_cannot_be_committed_is_refused_on_submit(requester):
    batch = drafted(requester, charges=False)
    response = ask(requester, "procurement.batch_commit", batch)
    assert response.status_code == 409 and response.json()["code"] == "batch_not_committable"
    assert not ChangeRequest.objects.exists()


def test_a_batch_edited_after_the_request_is_not_committed(requester, approver):
    batch = drafted(requester)
    body = ask(requester, "procurement.batch_commit", batch).json()
    assert requester.put(f"{BATCHES}{batch.uid}/charges/", {"charges": [{"kind": "FREIGHT", "amount": "9000"}]}, format="json").status_code == 200
    response = approver.post(f"{CHANGES}{body['uid']}/approve/", {}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "stale_version"
    assert Batch.objects.get(pk=batch.pk).status == BatchStatus.DRAFT and not Price.objects.exists()


def test_reverse_goes_through_approval_too(requester, approver):
    batch = drafted(requester)
    commit = ask(requester, "procurement.batch_commit", batch).json()
    assert approver.post(f"{CHANGES}{commit['uid']}/approve/", {}, format="json").status_code == 200
    batch.refresh_from_db()
    early = ask(requester, "procurement.batch_commit", batch)
    assert early.status_code == 409 and early.json()["code"] == "batch_not_draft"
    reverse = ask(requester, "procurement.batch_reverse", batch, reason="Wrong invoice")
    assert reverse.status_code == 201, reverse.json()
    assert not Batch.objects.filter(reverses=batch).exists()
    applied = approver.post(f"{CHANGES}{reverse.json()['uid']}/approve/", {}, format="json").json()
    reversal = Batch.objects.get(reverses=batch)
    assert applied["status"] == "APPROVED" and applied["result_uid"] == str(reversal.uid)
