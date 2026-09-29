"""``procurement/batches/`` — DRAFT authoring: header, lines, charges (PLAN §2.4, §3.4).

Only DRAFT batches change (409 ``batch_not_draft`` otherwise). Lines and charges are replaced with bulk PUTs (the
batch's ``version`` guards them: 409 ``stale_version``); every change recomputes ``subtotal`` (Σ qty × unit price),
``charges_total`` and ``total``. Numbers are ``BATCH-<YYYY>-<nnn>`` (``core.sequences``, per year). DELETE cancels a
DRAFT (status CANCELLED; the row and its number stay).
"""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from audit.services import changes, record, snapshot
from catalog.services import assert_selectable
from core.errors import Conflict, DomainError
from core.sequences import next_number
from core.services import check_version, stamp_create
from pricing.services.common import today
from procurement.models import Batch, BatchCharge, BatchLine, BatchStatus

HEADER_FIELDS = ("supplier", "invoice_no", "invoice_date", "note", "other_charges_declared")
SNAPSHOT_FIELDS = ("number", "supplier", "invoice_no", "invoice_date", "status", "subtotal", "charges_total", "total", "note")
TWO_PLACES = Decimal("0.01")
MAX_TOTAL = Decimal("999999999999.99")  # numeric(14,2): subtotal, charges_total, total


def batch_number(on=None) -> str:
    """The next free ``BATCH-<YYYY>-<nnn>``. Imported batches keep their source numbers (Flarize batch ids are free
    text), so a number the sequence reaches may already exist: it is skipped rather than refused (409 forever, since
    the refused transaction also rolls the counter back)."""
    year = (on or today()).year
    while True:
        number = next_number("BATCH", period_key=str(year), fmt=lambda n, period: f"BATCH-{period}-{n:03d}")
        if not Batch.all_objects.filter(number=number).exists():
            return number


def batches_queryset():
    return Batch.objects.select_related("supplier", "committed_by", "reverses").prefetch_related("reversals").order_by("-created_at", "-id")


def batch_detail_queryset():
    return batches_queryset().prefetch_related(
        Prefetch("lines", queryset=BatchLine.objects.select_related("component__category", "price_row", "landed_row").order_by("id")),
        Prefetch("charges", queryset=BatchCharge.objects.order_by("id")),
    )


def lock(batch: Batch, expected_version=None, *, draft: bool = True) -> Batch:
    locked = Batch.objects.select_for_update().get(pk=batch.pk)
    check_version(locked, expected_version)
    if draft and locked.status != BatchStatus.DRAFT:
        raise Conflict("batch_not_draft", f"Batch {locked.number} is {locked.status}; only DRAFT batches change. Correct a committed batch with a reversing batch.", errors={"status": [locked.status]})
    return locked


def recompute_totals(batch: Batch) -> dict:
    subtotal = sum((line.qty * line.unit_purchase_price for line in BatchLine.objects.filter(batch=batch)), Decimal("0")).quantize(TWO_PLACES)
    charges = sum((charge.amount for charge in BatchCharge.objects.filter(batch=batch)), Decimal("0")).quantize(TWO_PLACES)
    return {"subtotal": subtotal, "charges_total": charges, "total": (subtotal + charges).quantize(TWO_PLACES)}


def _number_conflict() -> Conflict:
    return Conflict("batch_number_taken", "This batch number is already used.")


@transaction.atomic
def create_batch(*, user, data: dict) -> Batch:
    supplier = data["supplier"]
    if not supplier.is_active:
        raise DomainError("supplier_inactive", "The supplier is inactive.", errors={"supplier_uid": ["Inactive supplier."]})
    values = {name: data[name] for name in HEADER_FIELDS if name in data}
    batch = Batch(number=batch_number(), status=BatchStatus.DRAFT, **values)
    stamp_create(batch, user)
    try:
        with transaction.atomic():
            batch.save()
    except IntegrityError:
        raise _number_conflict() from None
    record("procurement.batch_created", obj=batch, actor=user, after=snapshot(batch, SNAPSHOT_FIELDS))
    return batch


@transaction.atomic
def update_batch(instance: Batch, *, user, data: dict, expected_version=None) -> Batch:
    batch = lock(instance, expected_version)
    values = {name: data[name] for name in HEADER_FIELDS if name in data and getattr(batch, name) != data[name]}
    if "supplier" in values and not values["supplier"].is_active:
        raise DomainError("supplier_inactive", "The supplier is inactive.", errors={"supplier_uid": ["Inactive supplier."]})
    if not values:
        return batch
    before = snapshot(batch, SNAPSHOT_FIELDS)
    batch.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(batch, SNAPSHOT_FIELDS))
    record("procurement.batch_updated", obj=batch, actor=user, before=changed_before, after=changed_after)
    return batch


@transaction.atomic
def cancel_batch(instance: Batch, *, user, expected_version=None) -> None:
    batch = lock(instance, expected_version)
    batch.versioned_update(user, status=BatchStatus.CANCELLED)
    record("procurement.batch_cancelled", obj=batch, actor=user, before={"status": BatchStatus.DRAFT}, after={"status": BatchStatus.CANCELLED})


@transaction.atomic
def put_lines(instance: Batch, *, user, rows: list[dict], expected_version=None) -> Batch:
    """Replace the lines: ``[{component, qty, unit_purchase_price}]`` (components selectable, one line each)."""
    batch = lock(instance, expected_version)
    errors, seen = {}, set()
    for index, row in enumerate(rows):
        component = row["component"]
        try:
            assert_selectable(component, field=f"lines[{index}].component_uid")
        except DomainError as exc:
            errors.update(exc.errors)
        if component.pk in seen:
            errors[f"lines[{index}].component_uid"] = [f"{component.sku} appears twice; one line per component."]
        seen.add(component.pk)
        if row["qty"] <= 0:
            errors[f"lines[{index}].qty"] = ["Must be more than 0."]
        if row["unit_purchase_price"] < 0:
            errors[f"lines[{index}].unit_purchase_price"] = ["Must be ≥ 0."]
    if errors:
        raise DomainError("validation_error", "Invalid batch lines.", errors=errors)
    existing = {line.component_id: line for line in BatchLine.objects.filter(batch=batch)}
    for row in rows:
        line = existing.pop(row["component"].pk, None)
        if line is None:
            line = BatchLine(batch=batch, component=row["component"], qty=row["qty"], unit_purchase_price=row["unit_purchase_price"])
            stamp_create(line, user)
            line.save()
        elif line.qty != row["qty"] or line.unit_purchase_price != row["unit_purchase_price"]:
            line.versioned_update(user, qty=row["qty"], unit_purchase_price=row["unit_purchase_price"])
    for line in existing.values():
        line.soft_delete(user)
    return _after_change(batch, user=user, action="procurement.batch_lines_set", detail={"lines": len(rows)}, field="lines")


@transaction.atomic
def put_charges(instance: Batch, *, user, rows: list[dict], expected_version=None) -> Batch:
    """Replace the charges: ``[{kind, amount, note}]`` (supplier → warehouse; allocated by purchase value)."""
    batch = lock(instance, expected_version)
    errors = {f"charges[{index}].amount": ["Must be ≥ 0."] for index, row in enumerate(rows) if row["amount"] < 0}
    if errors:
        raise DomainError("validation_error", "Invalid batch charges.", errors=errors)
    for charge in BatchCharge.objects.filter(batch=batch):
        charge.soft_delete(user)
    for row in rows:
        charge = BatchCharge(batch=batch, kind=row["kind"], amount=row["amount"], note=row.get("note", ""))
        stamp_create(charge, user)
        charge.save()
    detail = {"charges": [{"kind": row["kind"], "amount": str(row["amount"])} for row in rows]}
    return _after_change(batch, user=user, action="procurement.batch_charges_set", detail=detail, field="charges")


def _after_change(batch: Batch, *, user, action: str, detail: dict, field: str) -> Batch:
    totals = recompute_totals(batch)
    if any(abs(value) > MAX_TOTAL for value in totals.values()):
        # numeric(14,2) cannot hold it; the caller's transaction rolls the line/charge writes back.
        raise DomainError("validation_error", "The batch totals are too large to record.", errors={field: [f"The batch total must stay within {MAX_TOTAL}."]})
    batch.versioned_update(user, **totals)
    record(action, obj=batch, actor=user, after={**detail, **{name: str(value) for name, value in totals.items()}})
    return batch
