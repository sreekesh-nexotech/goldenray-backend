"""Defects found by the adversarial review of procurement (each test failed before its fix).

* ``preview-allocation/`` showed callers without ``pricing_internal.view`` the per-line allocated charges and
  allocation share that the batch lines hide — the landed cost follows from them (unit price + charges / qty), also
  for committed batches.
* The next platform batch number was refused forever (409 ``batch_number_taken``, the counter rolled back with it)
  once an imported batch held it — Flarize batch ids are free text.
* Reversing a batch restored the price of an *earlier, already reversed* batch when that reversal had closed the
  price without a replacement.
* Batch totals that ``numeric(14,2)`` cannot hold were a 500.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from catalog.tests.factories import ComponentFactory
from pricing.models import Price, PriceKind
from procurement.models import Batch, BatchLine
from procurement.tests.factories import BatchChargeFactory, BatchFactory, BatchLineFactory, SupplierFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/procurement/batches/"


def detail(batch, suffix=""):
    return f"{URL}{batch.uid}/{suffix}"


def one_line_batch(component, price, *, supplier=None):
    batch = BatchFactory(supplier=supplier or SupplierFactory())
    BatchLineFactory(batch=batch, component=component, qty=Decimal("2"), unit_purchase_price=Decimal(price))
    BatchChargeFactory(batch=batch, amount=Decimal("100"))
    return batch


class TestAllocationPreviewGate:
    def test_allocation_is_hidden_without_pricing_internal(self, client, clerk):
        batch = one_line_batch(ComponentFactory(), "1000")
        for body in (clerk.post(detail(batch, "preview-allocation/")).json(),):
            line = body["lines"][0]
            assert not {"allocated_charges", "allocation_pct", "landed_unit_cost", "landed_unit_cost_exact", "formula"} & set(line)
            assert line["purchase_value"] == "2000.00" and body["can_commit"] is True
        assert client.post(detail(batch, "commit/"), {"reason": "x"}, format="json").status_code == 200
        committed = clerk.post(detail(batch, "preview-allocation/")).json()["lines"][0]
        assert "allocated_charges" not in committed and "allocation_pct" not in committed
        full = client.post(detail(batch, "preview-allocation/")).json()["lines"][0]
        assert full["allocated_charges"] == "100.00" and full["allocation_pct"] and full["landed_unit_cost"] == "1050.00"


class TestBatchNumbers:
    def test_numbers_held_by_imported_batches_are_skipped(self, client):
        year = timezone.localdate().year
        BatchFactory(number=f"BATCH-{year}-001")
        BatchFactory(number=f"BATCH-{year}-002")
        supplier = SupplierFactory()
        first = client.post(URL, {"supplier_uid": str(supplier.uid)}, format="json")
        assert first.status_code == 201, first.content
        assert first.json()["number"] == f"BATCH-{year}-003"
        assert client.post(URL, {"supplier_uid": str(supplier.uid)}, format="json").json()["number"] == f"BATCH-{year}-004"


class TestReversalChain:
    def test_reversing_a_batch_never_restores_a_reversed_price(self, client):
        component = ComponentFactory()
        first = one_line_batch(component, "1000")
        assert client.post(detail(first, "commit/"), {"reason": "first"}, format="json").status_code == 200
        undone = client.post(detail(first, "reverse/"), {"reason": "wrong supplier"}, format="json")
        assert undone.status_code == 201 and undone.json()["lines"][0]["purchase"] == "closed"
        assert not Price.objects.filter(component=component, effective_to__isnull=True).exists()
        second = one_line_batch(component, "1200", supplier=first.supplier)
        assert client.post(detail(second, "commit/"), {"reason": "second"}, format="json").status_code == 200
        response = client.post(detail(second, "reverse/"), {"reason": "wrong invoice"}, format="json")
        assert response.status_code == 201, response.content
        assert response.json()["lines"][0] == {"sku": component.sku, "purchase": "closed", "landed": "closed"}
        assert not Price.objects.filter(component=component, effective_to__isnull=True).exists()

    def test_a_price_before_the_reversed_batch_is_restored(self, client):
        component = ComponentFactory()
        base = one_line_batch(component, "900")
        client.post(detail(base, "commit/"), {"reason": "base"}, format="json")
        first = one_line_batch(component, "1000", supplier=base.supplier)
        client.post(detail(first, "commit/"), {"reason": "first"}, format="json")
        second = one_line_batch(component, "1200", supplier=base.supplier)
        client.post(detail(second, "commit/"), {"reason": "second"}, format="json")
        assert client.post(detail(first, "reverse/"), {"reason": "x"}, format="json").json()["lines"][0]["purchase"] == "superseded_since"
        body = client.post(detail(second, "reverse/"), {"reason": "y"}, format="json").json()
        assert body["lines"][0]["purchase"] == "restored"
        current = Price.objects.get(component=component, kind=PriceKind.PURCHASE, effective_to__isnull=True)
        assert current.amount == Decimal("900.00")  # the base batch's price; the reversed first batch's 1000 stays undone


class TestTotalsThatDoNotFit:
    def test_lines_and_charges_beyond_numeric_14_2_are_a_validation_error(self, client):
        batch = BatchFactory()
        component = ComponentFactory()
        huge = {"lines": [{"component_uid": str(component.uid), "qty": "999999999", "unit_purchase_price": "999999999999"}]}
        response = client.put(detail(batch, "lines/"), huge, format="json")
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "validation_error" and "lines" in response.json()["errors"]
        assert not BatchLine.objects.filter(batch=batch).exists()
        charges = {"charges": [{"kind": "FREIGHT", "amount": "999999999999.99"}, {"kind": "DUTY", "amount": "1"}]}
        refused = client.put(detail(batch, "charges/"), charges, format="json")
        assert refused.status_code == 400 and "charges" in refused.json()["errors"]
        batch.refresh_from_db()
        assert batch.total == Decimal("0") and Batch.objects.get(pk=batch.pk).version == 1
