"""procurement.services.legacy_import — Flarize procurement state, commercial history and price master.

Parity evidence (PLAN §7.6 #7): after the import every ``landedUnitCost``/``purchasePrice`` of the price master equals
the current LANDED/PURCHASE row (with the same version key), the history order is kept (the SEED-004 versions are
closed at SEED-005's effective date), and committing a platform batch built from the SEED-005 lines and its
₹15,000 delivery charge reproduces every landed unit cost Flarize recorded.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from catalog.models import Component
from catalog.tests.factories import ComponentFactory
from core.models import LegacyMap
from pricing.models import Price, PriceKind
from pricing.tests.legacy_fixtures import flarize_procurement
from procurement.models import Batch, BatchCharge, BatchLine, BatchStatus, Supplier
from procurement.services import legacy_import
from procurement.services.allocation import commit

pytestmark = pytest.mark.django_db


@pytest.fixture
def sources():
    state = flarize_procurement("procurement-state.json")
    history = flarize_procurement("commercial-history.json")
    master = flarize_procurement("procurement-price-master.json")
    for sku in sorted(master):
        ComponentFactory(sku=sku)
    return state, history, master


def test_import_matches_the_price_master(sources):
    state, history, master = sources
    result = legacy_import.import_flarize_procurement(state, master, history)
    assert [v for v in result["violations"] if v["severity"] == "error"] == []
    assert Supplier.objects.get().code == "SUP001"
    batches = {batch.number: batch for batch in Batch.objects.all()}
    assert {number: batch.status for number, batch in batches.items()} == {
        "BATCH-SEED-001": "DRAFT",
        "BATCH-SEED-002": "DRAFT",
        "BATCH-SEED-003": "DRAFT",
        "BATCH-SEED-004": "COMMITTED",
        "BATCH-SEED-005": "COMMITTED",
    }
    seed5 = batches["BATCH-SEED-005"]
    assert seed5.invoice_no == "PO-SEED-005" and seed5.effective_from == date(2026, 8, 30) and seed5.is_seed and seed5.commit_reason == "Initial price seed"
    assert list(BatchCharge.objects.filter(batch=seed5).values_list("kind", "amount")) == [("FREIGHT", Decimal("15000.00"))]
    assert batches["BATCH-SEED-003"].other_charges_declared == Decimal("2500.00")
    assert not BatchCharge.objects.filter(batch=batches["BATCH-SEED-001"]).exists()  # deliveryCost null = not entered
    current = {(row.component.sku, row.kind): row for row in Price.objects.filter(effective_to__isnull=True).select_related("component")}
    for sku, record in master.items():
        assert current[(sku, PriceKind.LANDED)].amount == Decimal(str(record["landedUnitCost"])), sku
        assert current[(sku, PriceKind.PURCHASE)].amount == Decimal(str(record["purchasePrice"])), sku
        assert current[(sku, PriceKind.LANDED)].version_key == record["landedCostVersion"]
    old = Price.objects.get(kind=PriceKind.LANDED, version_key="BATCH-SEED-004::a1")
    assert old.effective_to == date(2026, 8, 30) and old.note == "Landed = 3710 + (18 + 0) / 100"
    line = BatchLine.objects.get(batch=seed5, component__sku="a1")
    assert line.landed_row == current[("a1", PriceKind.LANDED)] and line.allocated_charges == Decimal("18.00") and line.allocation_pct == Decimal("0.1214")
    assert Price.objects.count() == 257 * 4 and not [v for v in result["violations"] if v["code"] == "price_master_differs"]
    rerun = legacy_import.import_flarize_procurement(state, master, history)
    assert rerun["created"] == 0 and Price.objects.count() == 257 * 4 and rerun["violations"] == result["violations"]


def test_committing_seed_005_reproduces_every_landed_cost(sources):
    state, _, master = sources
    seed = state["batches"]["BATCH-SEED-005"]
    supplier = Supplier.objects.create(code="SUP001", name="Master Supplier Co.")
    batch = Batch.objects.create(number="BATCH-PARITY-005", supplier=supplier, invoice_no="PO-SEED-005")
    components = {component.sku: component for component in Component.objects.all()}
    for item in seed["lines"]:
        BatchLine.objects.create(batch=batch, component=components[item["componentId"]], qty=Decimal(str(item["quantity"])), unit_purchase_price=Decimal(str(item["purchaseUnitPrice"])))
    BatchCharge.objects.create(batch=batch, kind="FREIGHT", amount=Decimal(str(seed["deliveryCost"])))
    commit(batch, user=None, reason="parity", effective_from=date(2026, 8, 30))
    landed = {row.component.sku: row.amount for row in Price.objects.filter(kind=PriceKind.LANDED, effective_to__isnull=True).select_related("component")}
    assert len(landed) == 257
    assert {sku: Decimal(str(record["landedUnitCost"])) for sku, record in master.items()} == landed
    history = flarize_procurement("commercial-history.json")["records"]
    allocated = {line.component.sku: line.allocated_charges for line in BatchLine.objects.filter(batch=batch).select_related("component")}
    assert allocated == {key.split("::")[1]: Decimal(str(versions[-1]["value"]["allocatedDeliveryCost"])) for key, versions in history.items()}
    assert sum(allocated.values()) == Decimal("15000.00")


def test_dry_run_and_bad_input(sources):
    state, history, master = sources
    small = {"suppliers": state["suppliers"], "batches": {"BATCH-SEED-001": state["batches"]["BATCH-SEED-001"]}}
    dry = legacy_import.import_flarize_procurement(small, None, None, dry_run=True)
    assert dry["created"] == 2 and not Batch.objects.exists() and not LegacyMap.objects.exists()
    broken = json.loads(json.dumps(small))
    broken["batches"]["BATCH-SEED-002"] = {**state["batches"]["BATCH-SEED-002"], "supplierId": "NOPE"}
    broken["batches"]["BATCH-SEED-003"] = {**state["batches"]["BATCH-SEED-003"], "allocationMethod": "WEIGHT"}
    seed1 = broken["batches"]["BATCH-SEED-001"]
    seed1["lines"][0]["otherProcurementCharges"] = [{"label": "x", "amount": 1}]
    seed1["lines"].append({"componentId": "ghost", "quantity": 1, "purchaseUnitPrice": 1})
    seed1["lines"].append({"componentId": "a1", "quantity": 0, "purchaseUnitPrice": 1})
    records = {key: history["records"][key] for key in ("PROCUREMENT_PRICE::a1", "PROCUREMENT_PRICE::a2")}
    other_history = json.loads(json.dumps({"records": records}))
    other_history["records"]["PROCUREMENT_PRICE::a1"][0]["value"]["purchasePrice"] = 1
    other_history["records"]["PROCUREMENT_PRICE::ghost"] = [{"versionId": "X::ghost", "value": {}, "effectiveFrom": "2026-08-30"}]
    legacy_import.import_flarize_procurement(broken, None, {"records": records})
    result = legacy_import.import_flarize_procurement(broken, {"a2": {**master["a2"], "landedUnitCost": 1}}, other_history)
    found = {v["code"] for v in result["violations"]}
    assert {"invalid_value", "component_not_found", "line_charges_not_supported", "history_differs", "price_master_differs"} <= found


def test_price_master_without_history(sources):
    _, _, master = sources
    result = legacy_import.import_flarize_procurement({"suppliers": {}, "batches": {}}, {"a1": master["a1"]})
    assert [v["code"] for v in result["violations"]] == ["master_without_history", "master_without_history"]
    assert Price.objects.get(kind=PriceKind.LANDED).version_key == "BATCH-SEED-005::a1"
    assert legacy_import.committed_landed_costs() == {"a1": Decimal("3710.00")}
    assert Batch.objects.filter(status=BatchStatus.COMMITTED).count() == 0
