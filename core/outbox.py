"""Transactional outbox (standard §7.2): the only way one bounded context triggers side effects in another.

Producer side — inside the service's transaction::

    emit("quotations.issued", {"quotation_uid": str(q.uid)}, aggregate_type="quotations.quotation", aggregate_uid=q.uid)

* the row is written in a **savepoint**, so a failed emit can never roll back the domain write (fail-soft);
* an optional ``dedup_key`` makes the emit idempotent (a second emit with the same key is a no-op);
* ``core.tasks.drain_outbox`` is enqueued **on commit** (Beat also drains every 5 s).

Consumer side — in ``<app>/events.py`` (autodiscovered at startup)::

    @handler("quotations.issued")
    def mark_lead_converted(event: Event) -> None: ...

Drain semantics:

* rows are claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` and marked processed **before** dispatch, so concurrent
  drainers and re-ticks never double-fire an event;
* each handler runs in its own transaction; handlers that succeeded are recorded in ``delivered`` and are never
  re-run on retry;
* a failure clears ``processed_at`` for a retry and records ``last_error``; after ``OUTBOX_MAX_ATTEMPTS`` (5) the row
  is parked (``parked_at``) so one poison pill cannot block the queue.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.module_loading import autodiscover_modules

logger = logging.getLogger("flarize.outbox")

EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class Event:
    id: int
    event_type: str
    aggregate_type: str
    aggregate_uid: uuid.UUID | None
    payload: dict[str, Any]
    created_at: datetime
    attempts: int


Handler = Callable[[Event], None]
_HANDLERS: dict[str, list[Handler]] = defaultdict(list)


def handler_name(fn: Handler) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


def handler(event_type: str):
    """Register ``fn`` for ``event_type``. Handlers must be idempotent and must not call ``emit`` recursively for the same event."""
    if not EVENT_TYPE_RE.match(event_type):
        raise ValueError(f"Invalid event type {event_type!r}; use '<context>.<name>'.")

    def decorator(fn: Handler) -> Handler:
        name = handler_name(fn)
        if all(handler_name(existing) != name for existing in _HANDLERS[event_type]):
            _HANDLERS[event_type].append(fn)
        return fn

    return decorator


def handlers_for(event_type: str) -> list[Handler]:
    return list(_HANDLERS.get(event_type, ()))


def unregister(event_type: str, fn: Handler) -> None:
    name = handler_name(fn)
    _HANDLERS[event_type] = [existing for existing in _HANDLERS.get(event_type, []) if handler_name(existing) != name]


def autodiscover_handlers() -> None:
    """Import ``<app>.events`` for every installed app (called from ``CoreConfig.ready``)."""
    autodiscover_modules("events")


def _normalise_payload(payload: dict | None) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise TypeError("Outbox payload must be a dict.")
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _schedule_drain() -> None:
    try:
        from core.tasks import drain_outbox

        drain_outbox.delay()
    except Exception:  # noqa: BLE001 - Beat drains every 5 s; a broker hiccup must not surface to the caller
        logger.warning("could not enqueue drain_outbox", exc_info=True)


def emit(event_type: str, payload: dict | None = None, *, aggregate_type: str = "", aggregate_uid=None, dedup_key: str | None = None):
    """Record an event in the caller's transaction. Returns the ``OutboxEvent`` or ``None`` (duplicate / failure)."""
    from core.models import OutboxEvent

    try:
        if not EVENT_TYPE_RE.match(event_type or ""):
            raise ValueError(f"Invalid event type {event_type!r}; use '<context>.<name>'.")
        data = _normalise_payload(payload)
        uid = uuid.UUID(str(aggregate_uid)) if aggregate_uid else None
    except (TypeError, ValueError):
        if getattr(settings, "OUTBOX_STRICT", False):
            raise
        logger.exception("outbox emit rejected (invalid event)", extra={"event_type": event_type})
        return None

    try:
        with transaction.atomic():
            event = OutboxEvent.objects.create(event_type=event_type, aggregate_type=aggregate_type or "", aggregate_uid=uid, payload=data, dedup_key=dedup_key or None)
    except IntegrityError:
        if dedup_key:
            logger.info("outbox emit deduplicated", extra={"event_type": event_type, "dedup_key": dedup_key})
            return None
        logger.exception("outbox emit failed", extra={"event_type": event_type})
        return None
    except Exception:  # noqa: BLE001 - fail-soft: the domain write must survive a failed emit
        logger.exception("outbox emit failed", extra={"event_type": event_type})
        return None

    transaction.on_commit(_schedule_drain, robust=True)
    return event


def _to_event(row) -> Event:
    return Event(
        id=row.id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_uid=row.aggregate_uid,
        payload=row.payload,
        created_at=row.created_at,
        attempts=row.attempts,
    )


def _claim(batch_size: int) -> list:
    from core.models import OutboxEvent

    now = timezone.now()
    with transaction.atomic():
        rows = list(OutboxEvent.objects.select_for_update(skip_locked=True).filter(processed_at__isnull=True, parked_at__isnull=True).order_by("id")[:batch_size])
        if rows:
            OutboxEvent.objects.filter(id__in=[row.id for row in rows]).update(processed_at=now, attempts=F("attempts") + 1)
            for row in rows:
                row.processed_at = now
                row.attempts += 1
    return rows


def _dispatch(row) -> tuple[list[str], BaseException | None]:
    """Run every not-yet-delivered handler. Returns (delivered handler names, first error)."""
    event = _to_event(row)
    delivered = list(row.delivered or [])
    for fn in handlers_for(row.event_type):
        name = handler_name(fn)
        if name in delivered:
            continue
        try:
            with transaction.atomic():
                fn(event)
        except Exception as exc:  # noqa: BLE001 - recorded on the row; the drain continues with other events
            logger.exception("outbox handler failed", extra={"event_type": row.event_type, "event_id": row.id, "handler": name})
            return delivered, exc
        delivered.append(name)
    return delivered, None


def _finish(row, delivered: list[str], error: BaseException | None) -> str:
    from core.models import OutboxEvent

    if error is None:
        OutboxEvent.objects.filter(id=row.id).update(delivered=delivered, last_error="")
        return "processed"
    message = f"{type(error).__name__}: {error}"[:4000]
    max_attempts = int(getattr(settings, "OUTBOX_MAX_ATTEMPTS", 5))
    if row.attempts >= max_attempts:
        OutboxEvent.objects.filter(id=row.id).update(delivered=delivered, last_error=message, processed_at=None, parked_at=timezone.now())
        from core.services.system_exceptions import record_exception

        record_exception(error, source="outbox", context={"event_id": row.id, "event_type": row.event_type, "attempts": row.attempts})
        return "parked"
    OutboxEvent.objects.filter(id=row.id).update(delivered=delivered, last_error=message, processed_at=None)
    return "failed"


def drain(batch_size: int | None = None) -> dict[str, int]:
    """Claim and dispatch up to ``batch_size`` pending events. Returns counts by outcome."""
    batch_size = batch_size or int(getattr(settings, "OUTBOX_BATCH_SIZE", 100))
    counts = {"claimed": 0, "processed": 0, "failed": 0, "parked": 0}
    rows = _claim(batch_size)
    counts["claimed"] = len(rows)
    for row in rows:
        delivered, error = _dispatch(row)
        counts[_finish(row, delivered, error)] += 1
    return counts


def drain_outbox_sync(max_rounds: int = 50) -> dict[str, int]:
    """Drain until nothing claimable is left (tests, management command). Failed rows are retried per round."""
    totals = {"claimed": 0, "processed": 0, "failed": 0, "parked": 0}
    for _ in range(max_rounds):
        counts = drain()
        for key, value in counts.items():
            totals[key] += value
        if counts["claimed"] == 0:
            break
    return totals


def backlog_stats() -> dict[str, Any]:
    """Pending count, oldest pending age (seconds) and parked count — used by /healthz and ops reports."""
    from django.db.models import Min

    from core.models import OutboxEvent

    pending = OutboxEvent.objects.filter(processed_at__isnull=True, parked_at__isnull=True)
    oldest = pending.aggregate(oldest=Min("created_at"))["oldest"]
    return {
        "pending": pending.count(),
        "oldest_age_seconds": int((timezone.now() - oldest).total_seconds()) if oldest else 0,
        "parked": OutboxEvent.objects.filter(parked_at__isnull=False).count(),
    }
