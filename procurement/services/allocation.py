"""Landed-cost allocation, commit and reversal of procurement batches (PLAN §2.4; DV-18).

``preview(batch)`` runs ``engines.cost.allocate_landed`` on the batch's lines and charges (the charges' sum is spread
by purchase-value proportion in one largest-remainder reconciliation; landed unit costs are whole rupees, the Flarize
rule) and lists the blockers of Flarize's allocation review: no lines, no charges entered (an absent delivery charge
is not zero — record a FREIGHT 0 if there was none), a declared other-charges control total that does not reconcile,
an engine refusal, an unreconciled allocation, retired components. Nothing is written.

``commit(batch)`` — one transaction: the preview must have no blockers, the batch needs an invoice/PO reference and a
reason; per line a PURCHASE row (the unit purchase price) and a LANDED row (the landed unit cost) are appended to
``pricing_price`` with ``version_key`` ``<number>::<sku>`` (closing the previous open rows), the line keeps the
allocation and links both rows, the batch becomes COMMITTED (immutable), an audit row and
``procurement.batch_committed`` are written.

``reverse(batch)`` — a COMMITTED batch is corrected by a reversing batch (COMMITTED at once, ``reverses`` → the
original): lines and charges mirror the original with negative quantities/amounts, and wherever the original's
PURCHASE/LANDED row is still the current price it is replaced by the row it had superseded (a new row with the old
amount, ``version_key`` ``<reversal number>::<sku>``) or simply closed when there was none. Prices a later batch
already replaced are left alone (reported ``superseded_since``). ``procurement.batch_reversed`` is emitted.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import record
from catalog.models import ComponentStatus
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import stamp_create
from engines import cost as cost_engine
from engines._jscompat import json_numbers
from flarize.cache_utils import bump
from pricing.models import Price, PriceKind, PriceSource
from pricing.services.common import AUTHORING_NAMESPACE, today
from pricing.services.prices import close_row, current_row, write_price
from procurement.models import Batch, BatchCharge, BatchLine, BatchStatus
from procurement.services.batches import batch_number, lock, recompute_totals

TWO_PLACES = Decimal("0.01")


def _engine_input(batch: Batch):
    lines = list(BatchLine.objects.filter(batch=batch).select_related("component").order_by("id"))
    charges = list(BatchCharge.objects.filter(batch=batch).order_by("id"))
    engine_lines = [json_numbers({"componentId": line.component.sku, "quantity": line.qty, "purchaseUnitPrice": line.unit_purchase_price}) for line in lines]
    engine_charges = [json_numbers({"kind": charge.kind, "amount": charge.amount}) for charge in charges]
    return lines, charges, engine_lines, engine_charges


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def preview(batch: Batch) -> dict:
    lines, charges, engine_lines, engine_charges = _engine_input(batch)
    result = cost_engine.allocate_landed(engine_lines, engine_charges, batch_id=batch.number)
    blockers = []
    if not lines:
        blockers.append({"code": "batch_has_no_lines", "message": "The batch has no material lines."})
    if not charges:
        blockers.append(
            {"code": "charges_not_entered", "message": "No supplier → warehouse charge was entered. It is not assumed to be zero: record what the supplier charged (a FREIGHT 0 if nothing)."}
        )
    if batch.other_charges_declared is not None and batch.other_charges_declared != 0:
        blockers.append(
            {
                "code": "other_charges_not_reconciled",
                "message": f"Other charges declared as {batch.other_charges_declared} but no line-level charges exist; enter them as batch charges and clear the declared total.",
            }
        )
    retired = [line.component.sku for line in lines if line.component.status == ComponentStatus.RETIRED or line.component.deleted_at is not None]
    if retired:
        blockers.append({"code": "component_retired", "message": f"Retired or deleted components: {', '.join(retired)}."})
    engine_errors = [{"code": error["code"], "message": error["message"]} for error in result.get("errors") or []]
    if lines and not result["ok"]:
        blockers.append({"code": "allocation_invalid", "message": "; ".join(error["message"] for error in engine_errors) or "The allocation engine refused the batch."})
    allocation = result.get("allocation") or {}
    if result["ok"] and not allocation.get("reconciled"):
        blockers.append({"code": "allocation_not_reconciled", "message": "The allocation does not sum exactly to the entered charges."})
    by_sku = {line.component.sku: line for line in lines}
    preview_lines = []
    for item in result.get("lines") or []:
        line = by_sku[item["componentId"]]
        landed = _decimal(item["landedUnitCost"])
        preview_lines.append(
            {
                "line": line,
                "component": line.component,
                "qty": line.qty,
                "unit_purchase_price": line.unit_purchase_price,
                "purchase_value": _decimal(item["purchaseValue"]),
                "allocation_pct": _decimal(item["allocationPct"]),
                "allocated_charges": _decimal(item["allocatedDeliveryCost"]),
                "landed_unit_cost": landed,
                "landed_unit_cost_exact": _decimal(item["landedUnitCostExact"]),
                "formula": item["formula"],
                "differs_from_purchase_price": landed != line.unit_purchase_price,
            }
        )
    return {
        "batch": batch,
        "ok": bool(result["ok"]),
        "can_commit": bool(result["ok"]) and not blockers,
        "blockers": blockers,
        "engine_errors": engine_errors,
        "charges_total": sum((charge.amount for charge in charges), Decimal("0")),
        "allocated_total": _decimal(allocation.get("allocatedTotal")) if result["ok"] else None,
        "total_purchase_value": _decimal(allocation.get("totalPurchaseValue")) if result["ok"] else None,
        "reconciled": bool(allocation.get("reconciled")) if result["ok"] else False,
        "lines": preview_lines,
    }


def _price_note(formula: str) -> str:
    return f"Landed = {formula}"


@transaction.atomic
def commit(instance: Batch, *, user, reason: str, effective_from=None, expected_version=None) -> Batch:
    batch = lock(instance, expected_version)
    if not (reason or "").strip():
        raise DomainError("reason_required", "Every commercial change records why: send a reason.", errors={"reason": ["Required."]})
    if not batch.invoice_no.strip():
        raise Conflict("invoice_reference_missing", "A batch needs the supplier's invoice / PO number before it becomes a price version.", errors={"invoice_no": ["Required to commit."]})
    effective_from = effective_from or today()
    if effective_from > today():
        raise DomainError("effective_from_in_future", "A price version is a recorded fact: it cannot start in the future.", errors={"effective_from": ["Must be today or earlier."]})
    result = preview(batch)
    if not result["can_commit"]:
        raise Conflict("batch_not_committable", "The batch cannot be committed yet.", errors={item["code"]: [item["message"]] for item in result["blockers"]})
    supplier = batch.supplier
    for item in result["lines"]:
        line = item["line"]
        component = item["component"]
        version_key = f"{batch.number}::{component.sku}"
        common = {"user": user, "source": PriceSource.BATCH, "effective_from": effective_from, "source_ref": str(line.uid), "supplier": supplier, "version_key": version_key}
        purchase, _ = write_price(component, PriceKind.PURCHASE, line.unit_purchase_price, note=f"{batch.number} {batch.invoice_no}".strip(), **common)
        landed, _ = write_price(component, PriceKind.LANDED, item["landed_unit_cost"], note=_price_note(item["formula"]), **common)
        line.versioned_update(
            user,
            landed_unit_cost=item["landed_unit_cost"],
            allocated_charges=item["allocated_charges"].quantize(TWO_PLACES) if item["allocated_charges"] is not None else None,
            allocation_pct=item["allocation_pct"],
            price_row=purchase,
            landed_row=landed,
        )
    totals = recompute_totals(batch)
    batch.versioned_update(
        user, status=BatchStatus.COMMITTED, committed_at=timezone.now(), committed_by=user if getattr(user, "pk", None) else None, effective_from=effective_from, commit_reason=reason, **totals
    )
    record(
        "procurement.batch_committed",
        obj=batch,
        actor=user,
        after={
            "number": batch.number,
            "lines": len(result["lines"]),
            "effective_from": str(effective_from),
            "allocated_total": str(result["allocated_total"]),
            **{k: str(v) for k, v in totals.items()},
        },
        note=reason,
    )
    emit(
        "procurement.batch_committed",
        {"batch_uid": str(batch.uid), "number": batch.number, "supplier_uid": str(supplier.uid), "lines": len(result["lines"]), "effective_from": str(effective_from)},
        aggregate_type="procurement.batch",
        aggregate_uid=batch.uid,
        dedup_key=f"procurement.batch_committed:{batch.uid}",
    )
    bump(AUTHORING_NAMESPACE)
    return batch


def _prior_row(row: Price) -> Price | None:
    """The row ``row`` superseded when it was written (the latest earlier row of the same component and kind)."""
    return Price.objects.filter(component_id=row.component_id, kind=row.kind, id__lt=row.pk).order_by("-id").first()


@transaction.atomic
def reverse(instance: Batch, *, user, reason: str, effective_from=None, expected_version=None) -> tuple[Batch, list[dict]]:
    original = lock(instance, expected_version, draft=False)
    if original.status != BatchStatus.COMMITTED:
        raise Conflict("batch_not_committed", f"Only COMMITTED batches are reversed; {original.number} is {original.status}.")
    if original.reverses_id is not None:
        raise Conflict("batch_is_reversal", "A reversing batch is not reversed again; commit a new batch instead.")
    if Batch.objects.filter(reverses=original).exists():
        raise Conflict("batch_already_reversed", f"{original.number} has already been reversed.")
    if not (reason or "").strip():
        raise DomainError("reason_required", "A reversal records why: send a reason.", errors={"reason": ["Required."]})
    effective_from = effective_from or today()
    if effective_from > today():
        raise DomainError("effective_from_in_future", "A reversal cannot start in the future.", errors={"effective_from": ["Must be today or earlier."]})
    if original.effective_from and effective_from < original.effective_from:
        raise DomainError("effective_from_before_batch", f"The reversal cannot take effect before the batch ({original.effective_from}).", errors={"effective_from": [str(original.effective_from)]})
    reversal = Batch(
        number=batch_number(),
        supplier=original.supplier,
        invoice_no=original.invoice_no,
        invoice_date=original.invoice_date,
        status=BatchStatus.DRAFT,
        note=f"Reversal of {original.number}",
        reverses=original,
    )
    stamp_create(reversal, user)
    reversal.save()
    for charge in BatchCharge.objects.filter(batch=original).order_by("id"):
        mirrored = BatchCharge(batch=reversal, kind=charge.kind, amount=-charge.amount, note=f"Reversal of {original.number}")
        stamp_create(mirrored, user)
        mirrored.save()
    outcome = []
    for line in BatchLine.objects.filter(batch=original).select_related("component", "price_row", "landed_row").order_by("id"):
        mirrored = BatchLine(batch=reversal, component=line.component, qty=-line.qty, unit_purchase_price=line.unit_purchase_price)
        stamp_create(mirrored, user)
        mirrored.save()
        links, entry = {}, {"sku": line.component.sku}
        for kind, row, column in ((PriceKind.PURCHASE, line.price_row, "price_row"), (PriceKind.LANDED, line.landed_row, "landed_row")):
            current = current_row(line.component, kind, lock=True)
            if row is None or current is None or current.pk != row.pk:
                entry[kind.lower()] = "superseded_since"
                continue
            prior = _prior_row(row)
            if prior is None:
                close_row(row, effective_from, user=user)
                entry[kind.lower()] = "closed"
                continue
            restored, _ = write_price(
                line.component,
                kind,
                prior.amount,
                user=user,
                source=PriceSource.BATCH,
                effective_from=effective_from,
                source_ref=str(mirrored.uid),
                supplier=prior.supplier,
                note=f"Reversal of {original.number}: restores {prior.version_key or prior.source}",
                version_key=f"{reversal.number}::{line.component.sku}",
                gst_inclusive=prior.gst_inclusive,
            )
            links[column] = restored
            entry[kind.lower()] = "restored"
        mirrored.versioned_update(user, landed_unit_cost=links["landed_row"].amount if "landed_row" in links else None, **links)
        outcome.append(entry)
    totals = recompute_totals(reversal)
    reversal.versioned_update(
        user, status=BatchStatus.COMMITTED, committed_at=timezone.now(), committed_by=user if getattr(user, "pk", None) else None, effective_from=effective_from, commit_reason=reason, **totals
    )
    record("procurement.batch_reversed", obj=original, actor=user, after={"reversal": reversal.number, "lines": outcome}, note=reason)
    emit(
        "procurement.batch_reversed",
        {"batch_uid": str(original.uid), "number": original.number, "reversal_uid": str(reversal.uid), "reversal_number": reversal.number},
        aggregate_type="procurement.batch",
        aggregate_uid=original.uid,
        dedup_key=f"procurement.batch_reversed:{original.uid}",
    )
    bump(AUTHORING_NAMESPACE)
    return reversal, outcome
