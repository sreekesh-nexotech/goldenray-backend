"""The procurement → inventory contract (reconciled at the wave-2a integration).

A batch committed through the real procurement service emits ``procurement.batch_committed`` in the shape inventory
books from (docs/decisions/inventory.md); with ``INVENTORY_STOCK`` on and a receiving location set, draining the
outbox turns every line into one PURCHASE movement. A reversal emits ``procurement.batch_reversed``, which inventory
does not consume (staff book the RETURN/ADJUST).
"""

from decimal import Decimal

import pytest

from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from inventory.models import BATCH_LINE_REF, Direction, Movement, Reason
from inventory.services.receiving import _lines
from procurement.models import BatchLine
from procurement.services import allocation
from procurement.tests.factories import BatchChargeFactory, BatchFactory, BatchLineFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def receiving_on(settings, store):
    settings.INVENTORY_RECEIVING_LOCATION = store.code


@pytest.fixture
def committed(keeper, component):
    batch = BatchFactory()
    BatchLineFactory(batch=batch, component=component, qty=Decimal("10.000"), unit_purchase_price=Decimal("7000.00"))
    BatchLineFactory(batch=batch, component=ComponentFactory(sku="INV-0002"), qty=Decimal("2.500"), unit_purchase_price=Decimal("4000.00"))
    BatchChargeFactory(batch=batch, amount=Decimal("1500.00"))
    return allocation.commit(batch, user=keeper, reason="September purchase")


def test_the_emitted_payload_satisfies_the_inventory_contract(committed, keeper):
    payload = OutboxEvent.objects.get(event_type="procurement.batch_committed").payload
    assert payload["batch_uid"] == str(committed.uid) and payload["number"] == committed.number
    assert payload["committed_by_uid"] == str(keeper.uid) and payload["imported"] is False and payload["committed_at"]
    parsed = _lines(payload)
    lines = {line.uid: line for line in BatchLine.objects.filter(batch=committed).select_related("component")}
    assert {(line_uid, component_uid, qty) for line_uid, component_uid, qty in parsed} == {(uid, line.component.uid, line.qty) for uid, line in lines.items()}


@pytest.mark.usefixtures("receiving_on")
def test_a_committed_batch_is_received_into_stock(committed, keeper, store, drain_outbox):
    drain_outbox()
    event = OutboxEvent.objects.get(event_type="procurement.batch_committed")
    assert event.processed_at is not None and event.parked_at is None
    movements = {movement.ref_uid: movement for movement in Movement.objects.all()}
    lines = list(BatchLine.objects.filter(batch=committed))
    assert set(movements) == {line.uid for line in lines}
    for line in lines:
        movement = movements[line.uid]
        assert (movement.component_id, movement.location, movement.qty) == (line.component_id, store, line.qty)
        assert (movement.direction, movement.reason, movement.ref_type) == (Direction.IN, Reason.PURCHASE, BATCH_LINE_REF)
        assert movement.by == keeper and movement.at == committed.committed_at and movement.note == f"Received with {committed.number}"


@pytest.mark.usefixtures("receiving_on")
def test_a_reversal_books_no_stock(committed, keeper, drain_outbox):
    drain_outbox()
    committed.refresh_from_db()
    reversal, _ = allocation.reverse(committed, user=keeper, reason="wrong invoice")
    drain_outbox()
    assert OutboxEvent.objects.get(event_type="procurement.batch_reversed").processed_at is not None
    assert not Movement.objects.filter(ref_uid__in=BatchLine.objects.filter(batch=reversal).values("uid")).exists()
    assert Movement.objects.count() == 2


def test_nothing_is_booked_without_a_receiving_location(committed, drain_outbox):
    drain_outbox()
    assert OutboxEvent.objects.get(event_type="procurement.batch_committed").processed_at is not None
    assert not Movement.objects.exists()
