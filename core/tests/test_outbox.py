import threading
import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings

from core import outbox
from core.models import FeatureFlag, OutboxEvent, SystemException
from core.outbox import Event, backlog_stats, drain, emit, handler, handlers_for, unregister


@pytest.fixture
def registry():
    """Register handlers for one test and remove them afterwards."""
    registered = []

    def _register(event_type, fn):
        handler(event_type)(fn)
        registered.append((event_type, fn))
        return fn

    yield _register
    for event_type, fn in registered:
        unregister(event_type, fn)


@pytest.mark.django_db
class TestEmit:
    def test_writes_a_row_with_json_safe_payload(self):
        uid = uuid.uuid4()
        event = emit("tests.created", {"uid": uid, "amount": Decimal("12.50")}, aggregate_type="tests.thing", aggregate_uid=uid)
        row = OutboxEvent.objects.get(pk=event.pk)
        assert row.event_type == "tests.created"
        assert row.payload == {"uid": str(uid), "amount": "12.50"}
        assert row.aggregate_uid == uid
        assert row.processed_at is None and row.attempts == 0

    def test_dedup_key_makes_emit_idempotent(self):
        assert emit("tests.created", {}, dedup_key="thing:1:created") is not None
        assert emit("tests.created", {}, dedup_key="thing:1:created") is None
        assert OutboxEvent.objects.filter(dedup_key="thing:1:created").count() == 1

    @pytest.mark.parametrize("event_type", ["", "nodot", "Upper.case", "a..b", "a.b-c"])
    def test_invalid_event_type_is_rejected(self, event_type):
        with pytest.raises(ValueError):
            emit(event_type, {})

    def test_non_dict_payload_is_rejected_in_strict_mode(self):
        with pytest.raises(TypeError):
            emit("tests.created", ["not", "a", "dict"])

    @override_settings(OUTBOX_STRICT=False)
    def test_invalid_events_are_dropped_fail_soft_outside_strict_mode(self):
        assert emit("bad", {}) is None
        assert emit("tests.created", {"x": object()}) is None
        assert OutboxEvent.objects.count() == 0

    def test_failed_emit_never_rolls_back_the_callers_write(self):
        with transaction.atomic():
            FeatureFlag.objects.create(key="LEGACY_API_SHIM", enabled=True)
            # NUL characters are rejected by Postgres jsonb: a genuine database error inside emit().
            assert emit("tests.created", {"text": "bad\x00value"}) is None
            FeatureFlag.objects.create(key="ADMS_RECEIVER", enabled=True)
        assert set(FeatureFlag.objects.values_list("key", flat=True)) == {"LEGACY_API_SHIM", "ADMS_RECEIVER"}
        assert OutboxEvent.objects.count() == 0

    def test_drain_is_enqueued_on_commit(self, registry, django_capture_on_commit_callbacks):
        seen = []
        registry("tests.committed", lambda event: seen.append(event.payload["n"]))
        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                emit("tests.committed", {"n": 1})
                assert seen == []
        assert seen == [1]
        assert OutboxEvent.objects.get().processed_at is not None


@pytest.mark.django_db
class TestDrain:
    def test_dispatches_an_event_object_and_marks_processed(self, registry):
        received: list[Event] = []
        registry("tests.drained", received.append)
        uid = uuid.uuid4()
        emit("tests.drained", {"a": 1}, aggregate_type="tests.thing", aggregate_uid=uid)
        assert drain() == {"claimed": 1, "processed": 1, "failed": 0, "parked": 0}
        assert len(received) == 1
        event = received[0]
        assert (event.event_type, event.payload, event.aggregate_uid, event.attempts) == ("tests.drained", {"a": 1}, uid, 1)
        row = OutboxEvent.objects.get()
        assert row.processed_at is not None and row.attempts == 1 and row.last_error == ""
        assert drain()["claimed"] == 0

    def test_row_is_marked_processed_before_dispatch(self, registry):
        states = []
        registry("tests.claimed", lambda event: states.append(OutboxEvent.objects.get(pk=event.id).processed_at is not None))
        emit("tests.claimed", {})
        drain()
        assert states == [True]

    def test_event_without_handlers_is_processed(self):
        emit("tests.nobody_listens", {})
        assert drain()["processed"] == 1

    def test_failed_handler_is_retried_without_rerunning_delivered_handlers(self, registry):
        calls = {"first": 0, "second": 0}

        def first(event):
            calls["first"] += 1

        def second(event):
            calls["second"] += 1
            if calls["second"] == 1:
                raise RuntimeError("temporary failure")

        registry("tests.retry", first)
        registry("tests.retry", second)
        emit("tests.retry", {})
        assert drain()["failed"] == 1
        row = OutboxEvent.objects.get()
        assert row.processed_at is None and row.attempts == 1
        assert "RuntimeError: temporary failure" in row.last_error
        assert row.delivered == [outbox.handler_name(first)]
        assert drain()["processed"] == 1
        assert calls == {"first": 1, "second": 2}
        row.refresh_from_db()
        assert row.processed_at is not None and row.last_error == ""
        assert row.delivered == [outbox.handler_name(first), outbox.handler_name(second)]

    def test_handler_writes_roll_back_on_failure(self, registry):
        def writes_then_fails(event):
            FeatureFlag.objects.create(key="INVENTORY_STOCK", enabled=True)
            raise RuntimeError("boom")

        registry("tests.partial", writes_then_fails)
        emit("tests.partial", {})
        drain()
        assert not FeatureFlag.objects.filter(key="INVENTORY_STOCK").exists()

    def test_poison_pill_is_parked_after_max_attempts(self, registry):
        registry("tests.poison", lambda event: 1 / 0)
        registry("tests.healthy", lambda event: None)
        emit("tests.poison", {})
        outcomes = [drain() for _ in range(5)]
        assert [outcome["failed"] for outcome in outcomes[:4]] == [1, 1, 1, 1]
        assert outcomes[4]["parked"] == 1
        row = OutboxEvent.objects.get(event_type="tests.poison")
        assert row.parked_at is not None and row.processed_at is None and row.attempts == 5
        assert "ZeroDivisionError" in row.last_error
        assert SystemException.objects.filter(source="outbox").count() == 1
        emit("tests.healthy", {})
        assert drain() == {"claimed": 1, "processed": 1, "failed": 0, "parked": 0}
        assert backlog_stats()["parked"] == 1

    def test_drain_outbox_sync_and_command(self, registry, drain_outbox, capsys):
        registry("tests.sync", lambda event: None)
        for _ in range(3):
            emit("tests.sync", {})
        assert drain_outbox()["processed"] == 3
        emit("tests.sync", {})
        call_command("drain_outbox")
        assert "processed=1" in capsys.readouterr().out

    def test_backlog_stats(self):
        assert backlog_stats() == {"pending": 0, "oldest_age_seconds": 0, "parked": 0}
        emit("tests.pending", {})
        assert backlog_stats()["pending"] == 1


def test_handler_registration_validates_and_deduplicates():
    with pytest.raises(ValueError):
        handler("NotValid")

    def fn(event):
        return None

    handler("tests.dedupe")(fn)
    handler("tests.dedupe")(fn)
    assert handlers_for("tests.dedupe") == [fn]
    unregister("tests.dedupe", fn)
    assert handlers_for("tests.dedupe") == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_drainers_never_double_dispatch(registry):
    counts: dict[int, int] = {}
    lock = threading.Lock()

    def count(event):
        with lock:
            counts[event.id] = counts.get(event.id, 0) + 1

    registry("tests.concurrent", count)
    for _ in range(40):
        emit("tests.concurrent", {})
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait()
            for _ in range(20):
                if drain(batch_size=5)["claimed"] == 0:
                    break
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(counts) == 40
    assert set(counts.values()) == {1}
    assert OutboxEvent.objects.filter(processed_at__isnull=True).count() == 0
