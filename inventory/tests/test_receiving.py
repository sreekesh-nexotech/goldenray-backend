"""procurement.batch_committed → PURCHASE movements into the receiving location (flag + setting, idempotent)."""

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError

from accounts.tests.factories import UserFactory
from audit.models import AuditLog
from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from core.outbox import emit
from inventory.models import BATCH_LINE_REF, Direction, Movement, Reason
from inventory.services import receiving
from inventory.services.receiving import ReceiptError, receive_batch
from inventory.tests.factories import LocationFactory

pytestmark = pytest.mark.django_db
RECEIVING = pytest.mark.usefixtures("receiving_on")


@pytest.fixture
def receiving_on(settings):
    settings.INVENTORY_RECEIVING_LOCATION = "ho-store"  # the store fixture's code, in another case


def batch(*lines, **extra):
    payload = {
        "batch_uid": str(uuid.uuid4()),
        "number": "BATCH-2026-004",
        "committed_at": "2026-09-20T10:15:00+05:30",
        "lines": [{"line_uid": str(uuid.uuid4()), "component_uid": str(component.uid), "qty": qty} for component, qty in lines],
    }
    payload.update(extra)
    return payload


@RECEIVING
class TestReceive:
    def test_every_positive_line_becomes_a_purchase(self, store, component):
        committer = UserFactory()
        other = ComponentFactory()
        payload = batch((component, "10.000"), (other, "2.5"), committed_by_uid=str(committer.uid))
        created = receive_batch(payload)
        assert len(created) == 2
        first = Movement.objects.get(component=component)
        assert (first.location, first.qty, first.direction, first.reason) == (store, Decimal("10.000"), Direction.IN, Reason.PURCHASE)
        assert first.ref_type == BATCH_LINE_REF and str(first.ref_uid) == payload["lines"][0]["line_uid"]
        assert first.by == committer and first.created_by is None
        assert first.at.isoformat() == "2026-09-20T04:45:00+00:00" and first.note == "Received with BATCH-2026-004"
        assert AuditLog.objects.filter(action="inventory.movement_recorded", actor_kind="SYSTEM").count() == 2

    def test_redelivery_books_nothing_twice(self, store, component):
        payload = batch((component, "4"))
        assert len(receive_batch(payload)) == 1
        assert receive_batch(payload) == []
        assert Movement.objects.count() == 1

    def test_the_unique_index_backs_up_a_racing_delivery(self, store, component):
        payload = batch((component, "4"))
        receive_batch(payload)
        with pytest.raises(IntegrityError, match="inventory_movement_batch_line_uniq"):
            Movement.objects.create(component=component, location=store, qty=1, direction="IN", reason="PURCHASE", ref_type=BATCH_LINE_REF, ref_uid=payload["lines"][0]["line_uid"])

    def test_a_racing_insert_is_skipped(self, store, component, monkeypatch):
        """Another drainer booked the line between our look-up and our insert: the unique index refuses it, we skip."""
        payload = batch((component, "4"))
        receive_batch(payload)
        monkeypatch.setattr(receiving, "booked_lines", lambda line_uids: set())
        assert receive_batch(payload) == []
        assert Movement.objects.count() == 1

    def test_other_integrity_errors_propagate(self, store, component, monkeypatch):
        def broken(**kwargs):
            raise IntegrityError("some other constraint")

        monkeypatch.setattr(receiving, "record_movement", broken)
        with pytest.raises(IntegrityError, match="some other constraint"):
            receive_batch(batch((component, "4")))

    def test_a_receipt_into_a_negative_balance_is_booked(self, store, component):
        """Stock issued before its purchase was received (balance -10): the receipt of 4 must still be booked (-6),
        otherwise the event is retried and parked and the batch never reaches the ledger."""
        Movement.objects.create(component=component, location=store, qty=10, direction="OUT", reason="ADJUST", note="issued before receipt")
        [movement] = receive_batch(batch((component, "4")))
        assert movement.balance_after == Decimal("-6.000") and movement.negative_override is False
        assert Movement.objects.filter(ref_type=BATCH_LINE_REF).count() == 1

    def test_zero_and_negative_lines_are_skipped(self, store, component):
        assert len(receive_batch(batch((component, "0"), (ComponentFactory(), "-3"), (ComponentFactory(), "1")))) == 1

    def test_imported_batches_book_nothing(self, store, component):
        assert receive_batch(batch((component, "4"), imported=True)) == []

    def test_a_soft_deleted_component_is_still_received(self, store, component):
        component.soft_delete()
        assert len(receive_batch(batch((component, "4")))) == 1

    def test_the_event_time_is_the_fallback_and_attribution_is_optional(self, store, component):
        from django.utils import timezone

        when = timezone.now().replace(microsecond=0)
        [movement] = receive_batch(batch((component, "1"), committed_at="yesterday", committed_by_uid="nope"), received_at=when)
        assert movement.at == when and movement.by is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"number": "B"},
            {"lines": "x"},
            {"lines": ["x"]},
            {"lines": [{"line_uid": "bad", "component_uid": str(uuid.uuid4()), "qty": "1"}]},
            {"lines": [{"line_uid": str(uuid.uuid4()), "component_uid": str(uuid.uuid4()), "qty": "many"}]},
            {"lines": [{"line_uid": str(uuid.uuid4()), "component_uid": str(uuid.uuid4()), "qty": "NaN"}]},
            {"lines": [{"line_uid": str(uuid.uuid4()), "component_uid": str(uuid.uuid4()), "qty": "1"}]},  # unknown component
        ],
    )
    def test_contract_violations_raise(self, store, payload):
        with pytest.raises(ReceiptError):
            receive_batch(payload)
        assert not Movement.objects.exists()

    def test_a_missing_receiving_location_raises(self, component):
        with pytest.raises(ReceiptError, match="not a live inventory location"):
            receive_batch(batch((component, "1")))


class TestSwitches:
    def test_off_without_a_receiving_location(self, settings, store, component):
        settings.INVENTORY_RECEIVING_LOCATION = "  "
        assert receive_batch(batch((component, "1"))) == []
        assert not Movement.objects.exists()

    @RECEIVING
    def test_off_while_the_flag_is_off(self, stock_off, store, component):
        assert receive_batch(batch((component, "1"))) == []


@RECEIVING
class TestThroughTheOutbox:
    def test_the_handler_books_the_batch(self, store, component, drain_outbox):
        emit("procurement.batch_committed", batch((component, "7")), aggregate_type="procurement.batch", aggregate_uid=uuid.uuid4())
        drain_outbox()
        movement = Movement.objects.get()
        assert movement.qty == 7 and movement.location == store
        assert OutboxEvent.objects.get(event_type="procurement.batch_committed").processed_at is not None

    def test_a_bad_payload_fails_visibly(self, component, drain_outbox):
        LocationFactory(code="ELSEWHERE")  # the configured code does not exist
        emit("procurement.batch_committed", batch((component, "7")), aggregate_type="procurement.batch", aggregate_uid=uuid.uuid4())
        drain_outbox()
        event = OutboxEvent.objects.get(event_type="procurement.batch_committed")
        assert event.processed_at is None and "not a live inventory location" in event.last_error
        assert not Movement.objects.exists()
        assert event.attempts == 1 and event.next_attempt_at is not None and event.parked_at is None  # retried with backoff, parked after OUTBOX_MAX_ATTEMPTS
