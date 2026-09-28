"""Transactional outbox (standard §7.2): the only way one bounded context triggers side effects in another.

Producer side — inside the service's transaction::

    emit("quotations.issued", {"quotation_uid": str(q.uid)}, aggregate_type="quotations.quotation", aggregate_uid=q.uid)

* the row is written in a **savepoint**, so a failed emit can never roll back the domain write (fail-soft);
* an optional ``dedup_key`` makes the emit idempotent (a second emit with the same key is a no-op);
* ``core.tasks.drain_outbox`` is enqueued **on commit** (Beat also drains every 5 s).

Consumer side — in ``<app>/events.py`` (autodiscovered at startup)::

    @handler("quotations.issued")
    def mark_lead_converted(event: Event) -> None: ...

Drain semantics (DV-7):

* rows are claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` and given a **lease** (``claimed_until``, now +
  ``OUTBOX_CLAIM_LEASE_SECONDS``) **before** dispatch, so concurrent drainers and Beat re-ticks never double-fire an
  event. The lease is renewed (compare-and-swap on ``claimed_until``) right before each row is dispatched: a row
  whose lease was taken over by another drainer is skipped (``lost``);
* ``processed_at`` is written only **after** every handler succeeded. A drainer that dies after claiming (SIGKILL,
  OOM, the Celery hard time limit) leaves a lease that expires; the row is then claimed again. Nothing is lost, and
  :func:`backlog_stats` reports expired leases (``stale_claims``) so ``/healthz`` degrades;
* each handler runs in its own transaction; handlers that succeeded are recorded in ``delivered`` and are never
  re-run on retry. Delivery is at-least-once: handlers must be idempotent;
* a failure releases the lease, records ``last_error`` and schedules the retry after an exponential backoff
  (``next_attempt_at``: ``OUTBOX_RETRY_BASE_SECONDS`` × 2^(attempt-1), capped at ``OUTBOX_RETRY_MAX_SECONDS``);
* after ``OUTBOX_MAX_ATTEMPTS`` (5) failed **or abandoned** claims the row is parked (``parked_at``) and a
  ``SystemException`` is recorded, so one poison pill (including an event that kills its worker) cannot block the
  queue. :func:`requeue_parked` (``manage.py drain_outbox --requeue-parked [--id N]``) re-drives parked rows once
  the handler is fixed.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F, Q
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


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def lease_duration() -> timedelta:
    """How long a claim protects a row. Longer than the Celery hard time limit, so a live drainer never loses it."""
    return timedelta(seconds=_setting("OUTBOX_CLAIM_LEASE_SECONDS", 180))


def retry_delay(attempts: int) -> timedelta:
    """Backoff before retry number ``attempts + 1``: base × 2^(attempts-1), capped."""
    base = _setting("OUTBOX_RETRY_BASE_SECONDS", 60)
    cap = _setting("OUTBOX_RETRY_MAX_SECONDS", 3600)
    return timedelta(seconds=min(cap, base * 2 ** max(0, attempts - 1)))


def _claimable(now: datetime) -> Q:
    return Q(processed_at__isnull=True, parked_at__isnull=True) & (Q(claimed_until__isnull=True) | Q(claimed_until__lt=now)) & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))


def _claim(batch_size: int) -> list:
    """Lease up to ``batch_size`` due rows (oldest first) and count the attempt. Returns the claimed rows."""
    from core.models import OutboxEvent

    now = timezone.now()
    lease = now + lease_duration()
    with transaction.atomic():
        rows = list(OutboxEvent.objects.select_for_update(skip_locked=True).filter(_claimable(now)).order_by("id")[:batch_size])
        if rows:
            OutboxEvent.objects.filter(id__in=[row.id for row in rows]).update(claimed_until=lease, attempts=F("attempts") + 1)
            for row in rows:
                row.claimed_until = lease
                row.attempts += 1
    return rows


def _held(row):
    """Queryset matching ``row`` only while this drainer still holds its claim (compare-and-swap on the lease)."""
    from core.models import OutboxEvent

    return OutboxEvent.objects.filter(id=row.id, claimed_until=row.claimed_until, processed_at__isnull=True, parked_at__isnull=True)


def _renew(row) -> bool:
    """Extend the lease right before dispatch. False when another drainer took the row over after our lease expired."""
    lease = timezone.now() + lease_duration()
    if not _held(row).update(claimed_until=lease):
        return False
    row.claimed_until = lease
    return True


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


def _park(row, error: BaseException, **columns) -> str:
    from core.services.system_exceptions import record_exception

    if not _held(row).update(parked_at=timezone.now(), claimed_until=None, next_attempt_at=None, **columns):
        return _lost(row)
    logger.error("outbox event parked", extra={"event_type": row.event_type, "event_id": row.id, "attempts": row.attempts})
    record_exception(error, source="outbox", context={"event_id": row.id, "event_type": row.event_type, "attempts": row.attempts})
    return "parked"


def _lost(row) -> str:
    logger.warning("outbox claim lost to another drainer", extra={"event_type": row.event_type, "event_id": row.id})
    return "lost"


def _finish(row, delivered: list[str], error: BaseException | None) -> str:
    """Record the outcome — only while the claim is still ours, otherwise the current claimant owns the row."""
    if error is None:
        updated = _held(row).update(processed_at=timezone.now(), claimed_until=None, next_attempt_at=None, delivered=delivered, last_error="")
        return "processed" if updated else _lost(row)
    message = f"{type(error).__name__}: {error}"[:4000]
    if row.attempts >= _setting("OUTBOX_MAX_ATTEMPTS", 5):
        return _park(row, error, delivered=delivered, last_error=message)
    updated = _held(row).update(claimed_until=None, next_attempt_at=timezone.now() + retry_delay(row.attempts), delivered=delivered, last_error=message)
    return "failed" if updated else _lost(row)


class AbandonedEvent(RuntimeError):
    """A row claimed ``OUTBOX_MAX_ATTEMPTS`` times whose last claim expired without an outcome (the drainer died)."""


def _abandon(row) -> str:
    error = AbandonedEvent(f"Outbox event #{row.id} ({row.event_type}) abandoned after {row.attempts - 1} claims; the last one expired without an outcome (the drainer died dispatching it).")
    previous = f" Last handler error: {row.last_error}" if row.last_error else ""
    return _park(row, error, last_error=f"{error}{previous}"[:4000])


def drain(batch_size: int | None = None) -> dict[str, int]:
    """Claim and dispatch up to ``batch_size`` due events. Returns counts by outcome."""
    batch_size = batch_size or _setting("OUTBOX_BATCH_SIZE", 100)
    max_attempts = _setting("OUTBOX_MAX_ATTEMPTS", 5)
    counts = {"claimed": 0, "processed": 0, "failed": 0, "parked": 0, "lost": 0}
    rows = _claim(batch_size)
    counts["claimed"] = len(rows)
    for row in rows:
        if row.attempts > max_attempts:
            counts[_abandon(row)] += 1
            continue
        if not _renew(row):
            counts[_lost(row)] += 1
            continue
        delivered, error = _dispatch(row)
        counts[_finish(row, delivered, error)] += 1
    return counts


def drain_outbox_sync(max_rounds: int = 50) -> dict[str, int]:
    """Drain until nothing is due (tests, management command). Failed rows wait for their backoff (a later drain)."""
    totals = {"claimed": 0, "processed": 0, "failed": 0, "parked": 0, "lost": 0}
    for _ in range(max_rounds):
        counts = drain()
        for key, value in counts.items():
            totals[key] += value
        if counts["claimed"] == 0:
            break
    return totals


@transaction.atomic
def requeue_parked(ids: list[int] | None = None) -> list[int]:
    """Re-drive parked rows (all, or only ``ids``) after the handler was fixed: attempts restart at 0.

    Handlers that already succeeded stay in ``delivered`` and are not re-run. Writes one audit row and schedules a
    drain after commit. Returns the requeued ids.
    """
    from audit.services import record
    from core.models import OutboxEvent

    parked = OutboxEvent.objects.select_for_update().filter(parked_at__isnull=False)
    if ids is not None:
        parked = parked.filter(id__in=ids)
    requeued = sorted(parked.values_list("id", flat=True))
    if not requeued:
        return []
    OutboxEvent.objects.filter(id__in=requeued).update(parked_at=None, attempts=0, claimed_until=None, next_attempt_at=None)
    record("core.outbox_requeued", object_type="core.outboxevent", after={"event_ids": requeued, "count": len(requeued)})
    transaction.on_commit(_schedule_drain, robust=True)
    logger.info("outbox parked events requeued", extra={"event_ids": requeued})
    return requeued


def backlog_stats() -> dict[str, Any]:
    """The outbox backlog for /healthz and the ops report.

    ``pending`` counts every event not yet processed or parked (including claimed and retrying rows) and
    ``oldest_age_seconds`` is the age of the oldest of them; ``stale_claims`` counts expired leases (a drainer died
    mid-batch); ``retrying`` counts rows waiting for their backoff; ``parked`` counts poison pills.
    """
    from django.db.models import Count, Min

    from core.models import OutboxEvent

    now = timezone.now()
    pending = OutboxEvent.objects.filter(processed_at__isnull=True, parked_at__isnull=True).aggregate(
        count=Count("id"),
        oldest=Min("created_at"),
        stale=Count("id", filter=Q(claimed_until__lt=now)),
        retrying=Count("id", filter=Q(next_attempt_at__gt=now)),
    )
    oldest = pending["oldest"]
    return {
        "pending": pending["count"],
        "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "stale_claims": pending["stale"],
        "retrying": pending["retrying"],
        "parked": OutboxEvent.objects.filter(parked_at__isnull=False).count(),
    }
