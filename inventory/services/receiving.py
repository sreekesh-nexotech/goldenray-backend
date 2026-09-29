"""Booking committed procurement batches into stock (outbox ``procurement.batch_committed``, optional).

Off unless **both** hold: the ``INVENTORY_STOCK`` flag is on, and ``settings.INVENTORY_RECEIVING_LOCATION`` names the
code of a live location (env ``INVENTORY_RECEIVING_LOCATION``, blank by default = off). Then every line of the batch
with a positive quantity becomes one IN / PURCHASE movement at that location: ``ref_type = procurement.batch_line``,
``ref_uid`` = the line uid, ``at`` = the commit time, ``by`` = the committer, note ``Received with <batch number>``.

Payload contract (the procurement package emits it; ``docs/decisions/inventory.md``)::

    {"batch_uid": "<uuid>", "number": "BATCH-2026-004", "committed_at": "<ISO 8601>", "committed_by_uid": "<uuid>|null",
     "lines": [{"line_uid": "<uuid>", "component_uid": "<uuid>", "qty": "10.000"}, …], "imported": false}

* idempotent — the outbox delivers at least once: a line already booked is skipped, and a partial unique index on
  ``(ref_type, ref_uid)`` for batch lines backs that up against two drainers racing on one event;
* ``imported: true`` (batches written by the Flarize importer) books nothing: history predates the ledger;
* a malformed payload, an unknown component, or a configured location that does not exist raises
  :class:`ReceiptError`; the outbox retries and then parks the event with a ``SystemException`` (visible in
  ``/healthz`` and the ops report). Fix the cause, then ``manage.py drain_outbox --requeue-parked``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from accounts.models import User
from catalog.models import Component
from inventory.models import BATCH_LINE_REF, Direction, Location, Movement, Reason
from inventory.services.common import receiving_location_code, stock_enabled
from inventory.services.locations import find_by_code
from inventory.services.movements import record_movement

logger = logging.getLogger("flarize.inventory")

__all__ = ["ReceiptError", "booked_lines", "receive_batch", "receiving_location", "receiving_location_code"]


class ReceiptError(ValueError):
    """The batch cannot be booked (contract violation or misconfiguration); the outbox retries, then parks it."""


def receiving_location() -> Location | None:
    """The configured receiving location, ``None`` when receiving is off; :class:`ReceiptError` when it is missing."""
    code = receiving_location_code()
    if not code:
        return None
    location = find_by_code(code)
    if location is None:
        raise ReceiptError(f"INVENTORY_RECEIVING_LOCATION {code!r} is not a live inventory location.")
    return location


def _uuid(value, what: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ReceiptError(f"procurement.batch_committed: {what} {value!r} is not a UUID.") from None


def _lines(payload: dict) -> list[tuple[uuid.UUID, uuid.UUID, Decimal]]:
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise ReceiptError("procurement.batch_committed payload carries no 'lines' list.")
    parsed = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise ReceiptError(f"procurement.batch_committed: line {index} is not an object.")
        try:
            qty = Decimal(str(line.get("qty")))
        except (InvalidOperation, TypeError, ValueError):
            raise ReceiptError(f"procurement.batch_committed: line {index} has no numeric qty.") from None
        if not qty.is_finite():
            raise ReceiptError(f"procurement.batch_committed: line {index} has no numeric qty.")
        parsed.append((_uuid(line.get("line_uid") or line.get("uid"), f"line {index} uid"), _uuid(line.get("component_uid"), f"line {index} component_uid"), qty))
    return parsed


def _committer(value) -> User | None:
    """The user who committed the batch (attribution only: an unknown or malformed uid books without one)."""
    try:
        uid = uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None
    return User.all_objects.filter(uid=uid).first() if uid else None


def _committed_at(payload: dict, default: datetime | None) -> datetime | None:
    value = payload.get("committed_at")
    parsed = parse_datetime(value) if isinstance(value, str) else None
    return parsed if parsed is not None and parsed.tzinfo is not None else default


def booked_lines(line_uids) -> set[uuid.UUID]:
    """The batch lines already received."""
    return set(Movement.objects.filter(ref_type=BATCH_LINE_REF, ref_uid__in=list(line_uids)).values_list("ref_uid", flat=True))


@transaction.atomic
def receive_batch(payload: dict, *, received_at: datetime | None = None) -> list[Movement]:
    """Book a committed batch into the receiving location. Returns the movements created by this call."""
    if not stock_enabled() or payload.get("imported") is True:
        return []
    location = receiving_location()
    if location is None:
        return []
    lines = _lines(payload)
    number = str(payload.get("number") or payload.get("batch_uid") or "").strip()
    committed_by = _committer(payload.get("committed_by_uid"))
    at = _committed_at(payload, received_at)
    booked = booked_lines([line_uid for line_uid, _, _ in lines])
    components = {component.uid: component for component in Component.all_objects.filter(uid__in=[component_uid for _, component_uid, _ in lines])}
    created = []
    for line_uid, component_uid, qty in lines:
        if line_uid in booked:
            continue
        if qty <= 0:  # a zero or reversing line is not a receipt; reversals are booked by staff as ADJUST/RETURN
            logger.info("batch line not booked (qty %s)", qty, extra={"line_uid": str(line_uid), "batch": number})
            continue
        component = components.get(component_uid)
        if component is None:
            raise ReceiptError(f"procurement.batch_committed: unknown component {component_uid} on line {line_uid}.")
        data = {
            "component": component,
            "location": location,
            "qty": qty,
            "direction": Direction.IN,
            "reason": Reason.PURCHASE,
            "ref_type": BATCH_LINE_REF,
            "ref_uid": line_uid,
            "at": at,
            "note": f"Received with {number}" if number else "Received from procurement",
        }
        try:
            with transaction.atomic():
                created.append(record_movement(user=None, data=data, by=committed_by, allow_reserved=True, include_deleted_component=True))
        except IntegrityError as exc:
            if "inventory_movement_batch_line_uniq" not in str(exc):
                raise
            continue  # a concurrent delivery of the same event booked it first
    return created
