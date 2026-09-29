"""``pricing_price`` writes and reads (PLAN §2.3, §3.4 ``pricing/prices/``, ``pricing/current/``).

* :func:`write_price` — THE way a price row is added (manual entry, procurement commit, reversal, import): the open
  row of the same component and kind is locked and closed (``effective_to`` = the new row's ``effective_from``) and the
  new row inserted, in the caller's transaction. One open row per kind is also a partial unique index, so a concurrent
  writer that slipped past the lock gets 409 ``price_conflict``.
* :func:`create_manual_price` — ``POST pricing/prices/``: LIST or LANDED rows only (PURCHASE comes from procurement
  batches); a LANDED row needs ``pricing_internal.view`` as well (nobody writes a cost they may not read).
* Reads hide PURCHASE and LANDED rows from callers without ``pricing_internal.view``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from audit.services import record
from catalog.models import Component, ComponentStatus
from core.errors import Conflict, DomainError, PermissionDenied, StaleVersion
from core.models import actor_or_none
from core.services import stamp_create
from flarize.cache_utils import bump
from pricing.models import CurrentPrice, Price, PriceKind, PriceSource
from pricing.services.common import AUTHORING_NAMESPACE, can_see_internal, json_safe, money_text, today

INTERNAL_KINDS = (PriceKind.PURCHASE, PriceKind.LANDED)
NOT_GIVEN = object()
MANUAL_KINDS = (PriceKind.LIST, PriceKind.LANDED)


def prices_queryset(user=None):
    queryset = Price.objects.select_related("component__category", "supplier", "created_by").order_by("component__sku", "kind", "-effective_from", "-id")
    if user is not None and not can_see_internal(user):
        queryset = queryset.exclude(kind__in=INTERNAL_KINDS)
    return queryset


def current_queryset(user=None):
    queryset = CurrentPrice.objects.select_related("component__category", "supplier").order_by("component__sku", "kind")
    if user is not None and not can_see_internal(user):
        queryset = queryset.exclude(kind__in=INTERNAL_KINDS)
    return queryset


def current_row(component: Component, kind: str, *, lock: bool = False) -> Price | None:
    queryset = Price.objects.filter(component=component, kind=kind, effective_to__isnull=True)
    if lock:
        queryset = queryset.select_for_update()
    return queryset.order_by("-effective_from", "-id").first()


def price_snapshot(row: Price) -> dict:
    return json_safe(
        {
            "component": row.component.sku if row.component_id else None,
            "kind": row.kind,
            "amount": money_text(row.amount),
            "currency": row.currency,
            "gst_inclusive": row.gst_inclusive,
            "per_watt": row.per_watt,
            "effective_from": row.effective_from,
            "source": row.source,
            "source_ref": row.source_ref,
            "version_key": row.version_key,
        }
    )


def close_row(row: Price, effective_to: date, *, user) -> Price:
    """Close an open row (the one column that may change; the database refuses anything else)."""
    if row.effective_to is not None:
        return row
    if effective_to < row.effective_from:
        effective_to = row.effective_from
    actor = actor_or_none(user)
    now = timezone.now()
    Price.all_objects.filter(pk=row.pk, effective_to__isnull=True).update(effective_to=effective_to, updated_at=now, updated_by=actor, version=F("version") + 1)
    row.effective_to = effective_to
    row.updated_at = now
    row.updated_by = actor
    row.version += 1
    return row


def write_price(
    component: Component,
    kind: str,
    amount: Decimal,
    *,
    user,
    source: str,
    effective_from: date | None = None,
    source_ref: str = "",
    supplier=None,
    note: str = "",
    version_key: str = "",
    gst_inclusive: bool = False,
    per_watt: Decimal | None = None,
    currency: str = "INR",
    expected_current_uid=NOT_GIVEN,
    allow_future: bool = False,
    created_at=None,
) -> tuple[Price, Price | None]:
    """Append a price row and close the previous open one (same transaction). Returns ``(new, previous)``."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("write_price() must run inside the caller's transaction.")
    if kind not in PriceKind.values:
        raise DomainError("validation_error", "Unknown price kind.", errors={"kind": [f"Must be one of {', '.join(PriceKind.values)}."]})
    if kind == PriceKind.LIST and source == PriceSource.MARKUP:
        raise DomainError("list_markup_forbidden", "A LIST price is never derived by markup (gross-margin pricing only).", errors={"source": ["MARKUP is not allowed for LIST."]})
    if amount is None or Decimal(amount) < 0:
        raise DomainError("validation_error", "The amount must be zero or more.", errors={"amount": ["Must be ≥ 0."]})
    effective_from = effective_from or today()
    if not allow_future and effective_from > today():
        raise DomainError("effective_from_in_future", "A price is a recorded fact: it cannot start in the future.", errors={"effective_from": ["Must be today or earlier."]})
    previous = current_row(component, kind, lock=True)
    if expected_current_uid is not NOT_GIVEN and str(previous.uid if previous else "") != str(expected_current_uid or ""):
        raise StaleVersion("stale_version", "The current price changed since you loaded it. Reload and try again.", errors={"expected_current_uid": [str(previous.uid) if previous else "none"]})
    if previous is not None and effective_from < previous.effective_from:
        raise DomainError(
            "effective_from_before_current",
            f"The current {kind} price is effective from {previous.effective_from}; a newer row cannot start earlier.",
            errors={"effective_from": [f"Must be on or after {previous.effective_from}."]},
        )
    if previous is not None:
        close_row(previous, effective_from, user=user)
    row = Price(
        component=component,
        kind=kind,
        amount=Decimal(amount).quantize(Decimal("0.01")),
        currency=currency,
        gst_inclusive=gst_inclusive,
        per_watt=per_watt,
        effective_from=effective_from,
        source=source,
        source_ref=source_ref[:64],
        supplier=supplier,
        note=note or "",
        version_key=version_key,
    )
    if created_at is not None:
        row.created_at = created_at
        row.updated_at = created_at
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:
        if "version_key" in str(exc):
            raise Conflict("price_version_exists", f"A {kind} price with version {version_key!r} already exists.", errors={"version_key": [version_key]}) from None
        raise Conflict("price_conflict", "Another current price was written for this component and kind at the same time; reload.") from None
    if created_at is not None:
        Price.all_objects.filter(pk=row.pk).update(updated_at=created_at)
    return row, previous


@transaction.atomic
def create_manual_price(*, user, data: dict) -> Price:
    component: Component = data["component"]
    kind = data["kind"]
    if kind not in MANUAL_KINDS:
        raise DomainError("price_kind_not_manual", "Only LIST and LANDED prices are entered by hand; PURCHASE prices come from procurement batches.", errors={"kind": ["LIST or LANDED."]})
    if kind == PriceKind.LANDED and not can_see_internal(user):
        raise PermissionDenied("pricing_internal_required", "Entering a landed cost needs the pricing_internal permission.")
    if component.status == ComponentStatus.RETIRED:
        raise Conflict("component_retired", "A retired component gets no new prices.", errors={"component_uid": ["Retired."]})
    row, previous = write_price(
        Component.objects.select_for_update().get(pk=component.pk),
        kind,
        data["amount"],
        user=user,
        source=PriceSource.MANUAL,
        effective_from=data.get("effective_from"),
        note=data.get("note", ""),
        gst_inclusive=data.get("gst_inclusive", False),
        per_watt=data.get("per_watt"),
        supplier=data.get("supplier"),
        expected_current_uid=data.get("expected_current_uid", NOT_GIVEN),
    )
    record("pricing.price_set", obj=row, actor=user, before=price_snapshot(previous) if previous else None, after=price_snapshot(row))
    bump(AUTHORING_NAMESPACE)
    return row
