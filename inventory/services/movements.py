"""``inventory/movements/`` — the append-only stock ledger (module ``inventory``: view / edit).

Rules of :func:`record_movement` (every refusal is a ``DomainError``):

* ``component`` is a live catalog component and ``location`` a live location, both by uid (400);
* ``qty`` is positive with at most 3 decimals (the direction gives the sign) (400);
* the reason fixes the direction: PURCHASE is IN, ISSUE_TO_PROJECT is OUT, RETURN and ADJUST go either way (400);
* ``ref_type`` (``<app>.<model>``) and ``ref_uid`` come together; ISSUE_TO_PROJECT needs one (which project?);
  ``procurement.batch_line`` is reserved for purchases booked from a committed procurement batch (400);
* ADJUST needs a ``note`` — an adjustment has no external reference, the note is its justification (400);
* ``at`` (when the stock moved, default now) may be in the past but not in the future (400);
* an OUT that would take the balance of the component at the location below zero is refused with 409
  ``insufficient_stock`` — unless it is an ADJUST (hence with a note) by a user holding ``inventory.edit``, which is
  recorded and audited as a negative-stock override.

Every movement of a location takes that location's row lock first, so two concurrent OUTs cannot both pass the
balance check, and a location cannot be deleted while a movement is being booked into it. Each movement writes one
audit row, emits ``inventory.movement_recorded`` and bumps the stock cache namespace.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounts.services.authz import can
from audit.services import record, snapshot
from catalog.models import Component
from core.errors import Conflict
from core.models.base import actor_or_none
from core.outbox import emit
from core.services import stamp_create
from inventory.models import BATCH_LINE_REF, REASON_DIRECTIONS, Balance, Direction, Location, Movement, Reason
from inventory.models.movement import REF_TYPE_PATTERN
from inventory.services.common import bump_stock, validation_error

QTY_PLACES = 3
MAX_QTY = Decimal("999999999.999")  # numeric(12,3)
FUTURE_TOLERANCE = timedelta(minutes=5)  # clock skew between the Studio and the server
SNAPSHOT_FIELDS = ("component", "location", "qty", "direction", "reason", "ref_type", "ref_uid", "at", "by", "note")
_REF_TYPE_RE = re.compile(REF_TYPE_PATTERN)


def movements_queryset():
    return Movement.objects.select_related("component__category", "location", "by").order_by("-at", "-id")


def balance_of(component, location) -> Decimal:
    """Current stock of ``component`` at ``location`` from the ``inventory_balance`` view (0 without movements)."""
    qty = Balance.objects.filter(component=component, location=location).values_list("qty", flat=True).first()
    return Decimal("0.000") if qty is None else qty


def _qty(value) -> Decimal:
    try:
        qty = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise validation_error({"qty": ["A valid number is required."]}) from None
    if not qty.is_finite() or qty <= 0:
        raise validation_error({"qty": ["Must be greater than zero; the direction gives the sign."]})
    if qty.as_tuple().exponent < -QTY_PLACES or qty > MAX_QTY:
        raise validation_error({"qty": [f"At most {QTY_PLACES} decimal places and {MAX_QTY}."]})
    return qty


def _uuid(value, field: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise validation_error({field: ["Must be a valid UUID."]}) from None


def _component(value, *, include_deleted: bool = False) -> Component:
    if isinstance(value, Component):
        component = value if include_deleted or value.deleted_at is None else None
    else:
        uid = _uuid(value, "component")
        manager = Component.all_objects if include_deleted else Component.objects
        component = manager.filter(uid=uid).first() if uid else None
    if component is None:
        raise validation_error({"component": ["Unknown component."]})
    return component


def _location_uid(value) -> uuid.UUID:
    uid = value.uid if isinstance(value, Location) else _uuid(value, "location")
    if uid is None:
        raise validation_error({"location": ["This field is required."]})
    return uid


def _check_reason(direction: str, reason: str) -> None:
    errors = {}
    if direction not in Direction.values:
        errors["direction"] = [f"One of {', '.join(Direction.values)}."]
    if reason not in Reason.values:
        errors["reason"] = [f"One of {', '.join(Reason.values)}."]
    if errors:
        raise validation_error(errors)
    if direction not in REASON_DIRECTIONS[reason]:
        raise validation_error({"direction": [f"{reason} is always {REASON_DIRECTIONS[reason][0]}."]})


def _check_reference(reason: str, ref_type: str, ref_uid, *, allow_reserved: bool) -> None:
    if bool(ref_type) != bool(ref_uid):
        field = "ref_uid" if ref_type else "ref_type"
        raise validation_error({field: ["ref_type and ref_uid are given together."]})
    if ref_type and not _REF_TYPE_RE.match(ref_type):
        raise validation_error({"ref_type": ["Use <app>.<model> in lower case, e.g. projects.project."]})
    if ref_type == BATCH_LINE_REF and not allow_reserved:
        raise validation_error({"ref_type": [f"{BATCH_LINE_REF} is booked from a committed procurement batch only."]})
    if reason == Reason.ISSUE_TO_PROJECT and not ref_type:
        raise validation_error({"ref_uid": ["ISSUE_TO_PROJECT names the project: give ref_type and ref_uid."]})


def _check_at(at):
    if at is None:
        return timezone.now()
    if not isinstance(at, datetime):
        raise validation_error({"at": ["A date-time is required."]})
    if timezone.is_naive(at):
        raise validation_error({"at": ["Give a time zone (ISO 8601 with offset)."]})
    if at > timezone.now() + FUTURE_TOLERANCE:
        raise validation_error({"at": ["Stock cannot move in the future."]})
    return at


def _lock_location(uid) -> Location:
    location = Location.objects.select_for_update(of=("self",)).filter(uid=uid).first()
    if location is None:
        raise validation_error({"location": ["Unknown location."]})
    return location


def _refuse_overdraw(component: Component, location: Location, balance: Decimal, qty: Decimal) -> Conflict:
    unit = component.effective_unit
    return Conflict(
        "insufficient_stock",
        f"Only {balance} {unit} of {component.sku} at {location.code}; an OUT of {qty} would leave {balance - qty}. Record an ADJUST with a note to book a negative balance.",
        errors={"qty": [f"Available: {balance}."]},
    )


@transaction.atomic
def record_movement(*, user, data, by=None, allow_reserved: bool = False, include_deleted_component: bool = False) -> Movement:
    """Append one movement. ``data``: ``component``, ``location`` (instances or uids), ``qty``, ``direction``,
    ``reason``, optional ``ref_type``, ``ref_uid``, ``at``, ``note``. ``by`` defaults to the acting user.

    Returns the saved movement carrying ``balance_after`` (the stock it left behind) and ``negative_override``.

    ``allow_reserved`` / ``include_deleted_component`` are for the procurement receipt only (a batch line reference;
    a component soft-deleted after its batch was committed was still delivered).
    """
    qty = _qty(data.get("qty"))
    direction, reason = str(data.get("direction") or ""), str(data.get("reason") or "")
    _check_reason(direction, reason)
    ref_type, ref_uid = (data.get("ref_type") or "").strip(), _uuid(data.get("ref_uid"), "ref_uid")
    _check_reference(reason, ref_type, ref_uid, allow_reserved=allow_reserved)
    note = (data.get("note") or "").strip()
    if reason == Reason.ADJUST and not note:
        raise validation_error({"note": ["An ADJUST needs a note (why the stock is corrected)."]})
    at = _check_at(data.get("at"))
    component = _component(data.get("component"), include_deleted=include_deleted_component)
    location = _lock_location(_location_uid(data.get("location")))

    balance = balance_of(component, location)
    after = balance + qty if direction == Direction.IN else balance - qty
    override = False
    if after < 0:
        if reason != Reason.ADJUST or not can(user, "inventory", "edit"):
            raise _refuse_overdraw(component, location, balance, qty)
        override = True

    movement = Movement(
        component=component,
        location=location,
        qty=qty,
        direction=direction,
        reason=reason,
        ref_type=ref_type,
        ref_uid=ref_uid,
        at=at,
        by=by if by is not None else actor_or_none(user),
        note=note,
    )
    stamp_create(movement, user)
    movement.save()
    after_snapshot = {**snapshot(movement, SNAPSHOT_FIELDS), "balance_before": str(balance), "balance_after": str(after)}
    if override:
        after_snapshot["negative_override"] = True
    record(
        "inventory.movement_recorded",
        obj=movement,
        actor=user,
        after=after_snapshot,
        note=f"negative-stock override: {note}" if override else note,
    )
    emit(
        "inventory.movement_recorded",
        {
            "movement_uid": str(movement.uid),
            "component_uid": str(component.uid),
            "location_uid": str(location.uid),
            "direction": direction,
            "reason": reason,
            "qty": str(qty),
            "balance_after": str(after),
            "ref_type": ref_type,
            "ref_uid": str(ref_uid) if ref_uid else None,
        },
        aggregate_type="inventory.movement",
        aggregate_uid=movement.uid,
    )
    bump_stock()
    movement.balance_after = after
    movement.negative_override = override
    return movement
