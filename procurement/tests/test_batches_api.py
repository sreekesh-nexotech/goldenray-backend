"""procurement/batches/ — DRAFT authoring, allocation preview, commit (PURCHASE + LANDED rows), reversal."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from audit.models import AuditLog
from catalog.models import ComponentStatus
from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from pricing.models import Price, PriceKind
from pricing.tests.factories import PriceFactory
from procurement.models import Batch, BatchLine, BatchStatus
from procurement.tests.factories import BatchChargeFactory, BatchFactory, BatchLineFactory, SupplierFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/procurement/batches/"


def detail(batch, suffix=""):
    return f"{URL}{batch.uid}/{suffix}"


def prepared(client, *, charges=("5000",)):
    """A DRAFT batch with three lines (70k / 20k / 10k purchase value) and the given FREIGHT charges."""
    supplier = SupplierFactory()
    batch_uid = client.post(URL, {"supplier_uid": str(supplier.uid), "invoice_no": "PO-1", "invoice_date": "2026-09-01"}, format="json").json()["uid"]
    batch = Batch.objects.get(uid=batch_uid)
    a, b, c = ComponentFactory(sku="a1"), ComponentFactory(sku="b1"), ComponentFactory(sku="c1")
    lines = [
        {"component_uid": str(a.uid), "qty": "10", "unit_purchase_price": "7000"},
        {"component_uid": str(b.uid), "qty": "4", "unit_purchase_price": "5000"},
        {"component_uid": str(c.uid), "qty": "1", "unit_purchase_price": "10000"},
    ]
    assert client.put(detail(batch, "lines/"), {"lines": lines}, format="json").status_code == 200
    if charges:
        assert client.put(detail(batch, "charges/"), {"charges": [{"kind": "FREIGHT", "amount": amount} for amount in charges]}, format="json").status_code == 200
    batch.refresh_from_db()
    return batch, (a, b, c)


class TestPermissions:
    def test_anonymous_outsider_viewer_clerk(self, api_client, outsider, viewer, clerk):
        batch = BatchFactory()
        assert api_client.get(URL).status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, {}, format="json").status_code == 403
        assert viewer.put(detail(batch, "lines/"), {"lines": []}, format="json").status_code == 403
        assert clerk.post(detail(batch, "commit/"), {"reason": "x"}, format="json").status_code == 403
        assert clerk.post(detail(batch, "reverse/"), {"reason": "x"}, format="json").status_code == 403
        assert viewer.post(detail(batch, "preview-allocation/")).status_code == 200


class TestDraftAuthoring:
    def test_create_number_lines_charges_totals(self, client, procurement_user):
        batch, _ = prepared(client, charges=("3000", "2000"))
        year = timezone.localdate().year
        assert batch.number == f"BATCH-{year}-001" and batch.status == BatchStatus.DRAFT
        assert (batch.subtotal, batch.charges_total, batch.total) == (Decimal("100000.00"), Decimal("5000.00"), Decimal("105000.00"))
        body = client.get(detail(batch)).json()
        assert len(body["lines"]) == 3 and body["lines"][0]["line_value"] == "70000.00" and len(body["charges"]) == 2
        assert client.get(detail(batch, "lines/")).json()["count"] == 3
        assert client.get(detail(batch, "charges/")).json()["count"] == 2
        second = client.post(URL, {"supplier_uid": str(batch.supplier.uid)}, format="json").json()
        assert second["number"] == f"BATCH-{year}-002"
        assert AuditLog.objects.filter(action="procurement.batch_lines_set", actor=procurement_user).exists()

    def test_line_validation(self, client):
        batch, (a, _, _) = prepared(client)
        retired = ComponentFactory(status=ComponentStatus.RETIRED)
        bad = [
            {"component_uid": str(a.uid), "qty": "0", "unit_purchase_price": "1"},
            {"component_uid": str(a.uid), "qty": "1", "unit_purchase_price": "-1"},
            {"component_uid": str(retired.uid), "qty": "1", "unit_purchase_price": "1"},
        ]
        response = client.put(detail(batch, "lines/"), {"lines": bad}, format="json")
        assert response.status_code == 400
        errors = response.json()["errors"]
        assert {"lines[0].qty", "lines[1].unit_purchase_price", "lines[1].component_uid", "lines[2].component_uid"} <= set(errors)
        charges = client.put(detail(batch, "charges/"), {"charges": [{"kind": "FREIGHT", "amount": "-5"}]}, format="json")
        assert charges.status_code == 400
        assert client.put(detail(batch, "charges/"), {"charges": [{"kind": "TIPS", "amount": "5"}]}, format="json").status_code == 400

    def test_stale_version_and_patch(self, client):
        batch, _ = prepared(client)
        stale = client.put(detail(batch, "lines/"), {"lines": [], "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        patched = client.patch(detail(batch), {"invoice_no": "PO-2", "expected_version": batch.version}, format="json")
        assert patched.status_code == 200 and patched.json()["invoice_no"] == "PO-2"
        inactive = SupplierFactory(is_active=False)
        assert client.patch(detail(batch), {"supplier_uid": str(inactive.uid)}, format="json").json()["code"] == "supplier_inactive"
        assert client.post(URL, {"supplier_uid": str(inactive.uid)}, format="json").json()["code"] == "supplier_inactive"

    def test_cancel_draft(self, client):
        batch, _ = prepared(client)
        assert client.delete(detail(batch)).status_code == 204
        batch.refresh_from_db()
        assert batch.status == BatchStatus.CANCELLED
        again = client.patch(detail(batch), {"note": "x"}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "batch_not_draft"

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for _ in range(12):
            BatchFactory()
        with django_assert_max_num_queries(10):
            assert client.get(URL, {"page_size": 50}).json()["count"] == 12

    def test_children_query_budget(self, client, django_assert_max_num_queries):
        batch = BatchFactory()
        for _ in range(12):
            BatchLineFactory(batch=batch)
            BatchChargeFactory(batch=batch)
        for suffix in ("lines/", "charges/"):
            with django_assert_max_num_queries(10):
                assert client.get(detail(batch, suffix), {"page_size": 50}).json()["count"] == 12

    def test_put_on_the_batch_itself_is_not_allowed(self, client):
        batch = BatchFactory()
        assert client.put(detail(batch), {"note": "x"}, format="json").status_code == 405
        assert client.patch(detail(batch), {"note": "x", "expected_version": 1}, format="json").status_code == 200


class TestPreviewAndCommit:
    def test_preview_allocates_by_purchase_value(self, client, clerk):
        batch, _ = prepared(client)
        body = client.post(detail(batch, "preview-allocation/")).json()
        assert body["can_commit"] is True and body["reconciled"] is True and body["allocated_total"] == "5000.00"
        by_sku = {line["component"]["sku"]: line for line in body["lines"]}
        assert [by_sku[sku]["allocated_charges"] for sku in ("a1", "b1", "c1")] == ["3500.00", "1000.00", "500.00"]
        assert by_sku["a1"]["landed_unit_cost"] == "7350.00" and by_sku["b1"]["landed_unit_cost"] == "5250.00"
        hidden = clerk.post(detail(batch, "preview-allocation/")).json()
        assert "landed_unit_cost" not in hidden["lines"][0] and hidden["lines"][0]["allocated_charges"]

    def test_blockers(self, client):
        empty = BatchFactory()
        body = client.post(detail(empty, "preview-allocation/")).json()
        assert {item["code"] for item in body["blockers"]} == {"batch_has_no_lines", "charges_not_entered"}
        refused = client.post(detail(empty, "commit/"), {"reason": "seed"}, format="json")
        assert refused.status_code == 409 and refused.json()["code"] == "batch_not_committable"
        batch, (a, _, _) = prepared(client, charges=())
        Batch.objects.filter(pk=batch.pk).update(other_charges_declared=Decimal("2500"))
        a.status = ComponentStatus.RETIRED
        a.save()
        codes = {item["code"] for item in client.post(detail(batch, "preview-allocation/")).json()["blockers"]}
        assert codes == {"charges_not_entered", "other_charges_not_reconciled", "component_retired"}

    def test_commit_writes_price_rows_and_is_immutable(self, client, procurement_user):
        batch, (a, b, c) = prepared(client)
        old = PriceFactory(component=a, kind=PriceKind.LANDED, amount=Decimal("7000"), effective_from=date(2026, 1, 1))
        missing = client.post(detail(batch, "commit/"), {}, format="json")
        assert missing.status_code == 400 and "reason" in missing.json()["errors"]
        response = client.post(detail(batch, "commit/"), {"reason": "September purchase", "expected_version": batch.version}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["status"] == "COMMITTED" and body["effective_from"] == str(timezone.localdate()) and body["commit_reason"] == "September purchase"
        rows = {(row.component.sku, row.kind): row for row in Price.objects.filter(effective_to__isnull=True).select_related("component")}
        assert rows[("a1", "PURCHASE")].amount == Decimal("7000.00") and rows[("a1", "LANDED")].amount == Decimal("7350.00")
        assert rows[("a1", "LANDED")].version_key == f"{batch.number}::a1" and rows[("a1", "LANDED")].supplier == batch.supplier
        old.refresh_from_db()
        assert old.effective_to == timezone.localdate()
        line = BatchLine.objects.get(batch=batch, component=a)
        assert line.price_row == rows[("a1", "PURCHASE")] and line.landed_row == rows[("a1", "LANDED")] and line.allocated_charges == Decimal("3500.00")
        assert OutboxEvent.objects.get(event_type="procurement.batch_committed").payload["lines"] == 3
        assert AuditLog.objects.get(action="procurement.batch_committed").actor == procurement_user
        immutable = client.put(detail(batch, "lines/"), {"lines": []}, format="json")
        assert immutable.status_code == 409 and immutable.json()["code"] == "batch_not_draft"
        again = client.post(detail(batch, "commit/"), {"reason": "again"}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "batch_not_draft"

    def test_commit_rules(self, client):
        batch, _ = prepared(client)
        Batch.objects.filter(pk=batch.pk).update(invoice_no="")
        no_invoice = client.post(detail(batch, "commit/"), {"reason": "x"}, format="json")
        assert no_invoice.status_code == 409 and no_invoice.json()["code"] == "invoice_reference_missing"
        Batch.objects.filter(pk=batch.pk).update(invoice_no="PO-1")
        future = client.post(detail(batch, "commit/"), {"reason": "x", "effective_from": str(timezone.localdate() + timedelta(days=2))}, format="json")
        assert future.status_code == 400 and future.json()["code"] == "effective_from_in_future"
        stale = client.post(detail(batch, "commit/"), {"reason": "x", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_landed_fields_hidden_without_pricing_internal(self, client, clerk):
        batch, _ = prepared(client)
        client.post(detail(batch, "commit/"), {"reason": "x"}, format="json")
        line = clerk.get(detail(batch)).json()["lines"][0]
        assert "landed_unit_cost" not in line and "allocated_charges" not in line and line["unit_purchase_price"]
        assert "landed_unit_cost" in client.get(detail(batch)).json()["lines"][0]


class TestReverse:
    def test_reversal_restores_superseded_prices(self, client):
        batch, (a, b, c) = prepared(client)
        previous = PriceFactory(component=a, kind=PriceKind.LANDED, amount=Decimal("7000"), effective_from=date(2026, 1, 1), version_key="OLD::a1")
        client.post(detail(batch, "commit/"), {"reason": "x"}, format="json")
        batch.refresh_from_db()
        response = client.post(detail(batch, "reverse/"), {"reason": "wrong invoice", "expected_version": batch.version}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        reversal = Batch.objects.get(uid=body["reversal"]["uid"])
        assert reversal.status == BatchStatus.COMMITTED and reversal.reverses == batch
        assert (reversal.subtotal, reversal.charges_total) == (Decimal("-100000.00"), Decimal("-5000.00"))
        outcome = {line["sku"]: line for line in body["lines"]}
        assert outcome["a1"] == {"sku": "a1", "purchase": "closed", "landed": "restored"}
        current = {(row.component.sku, row.kind): row for row in Price.objects.filter(effective_to__isnull=True).select_related("component")}
        assert current[("a1", "LANDED")].amount == previous.amount and current[("a1", "LANDED")].version_key == f"{reversal.number}::a1"
        assert ("a1", "PURCHASE") not in current and ("b1", "LANDED") not in current
        assert OutboxEvent.objects.filter(event_type="procurement.batch_reversed").count() == 1
        twice = client.post(detail(batch, "reverse/"), {"reason": "again"}, format="json")
        assert twice.status_code == 409 and twice.json()["code"] == "batch_already_reversed"
        of_reversal = client.post(detail(reversal, "reverse/"), {"reason": "x"}, format="json")
        assert of_reversal.status_code == 409 and of_reversal.json()["code"] == "batch_is_reversal"
        detail_body = client.get(detail(batch)).json()
        assert detail_body["reversed_by"]["number"] == reversal.number

    def test_reversal_leaves_later_prices_alone(self, client):
        first, (a, _, _) = prepared(client)
        client.post(detail(first, "commit/"), {"reason": "x"}, format="json")
        later = BatchFactory(supplier=first.supplier)
        BatchLineFactory(batch=later, component=a, qty=Decimal("1"), unit_purchase_price=Decimal("8000"))
        BatchChargeFactory(batch=later, amount=Decimal("0"))
        assert client.post(detail(later, "commit/"), {"reason": "y"}, format="json").status_code == 200
        body = client.post(detail(first, "reverse/"), {"reason": "z"}, format="json").json()
        assert {line["sku"]: line["landed"] for line in body["lines"]}["a1"] == "superseded_since"
        assert Price.objects.get(component=a, kind=PriceKind.LANDED, effective_to__isnull=True).amount == Decimal("8000.00")

    def test_only_committed_batches(self, client):
        draft = BatchFactory()
        response = client.post(detail(draft, "reverse/"), {"reason": "x"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "batch_not_committed"
        batch, _ = prepared(client)
        client.post(detail(batch, "commit/"), {"reason": "x"}, format="json")
        assert client.post(detail(batch, "reverse/"), {"reason": ""}, format="json").status_code == 400
