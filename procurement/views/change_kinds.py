"""Procurement price changes that go through a change request (``approvals``, DV-155).

Committing a batch writes a PURCHASE and a LANDED price version for every line; reversing one restores the prices it
superseded. Both are Procurement changing prices, so a Procurement user asks and an Admin (``procurement.commit``)
approves — the approval runs :func:`procurement.services.allocation.commit` / ``reverse`` as the Admin. Editing a DRAFT
batch (lines, charges) stays direct: nothing is priced until it is committed.

The request is checked when it is made (the batch must be committable / reversible now) and the batch's ``version``
is pinned: a batch edited after the request is refused on approval (409 ``stale_version``) rather than committed with
lines the Admin never saw.
"""

from __future__ import annotations

from approvals.services.kinds import UPDATE, ChangeKind, json_safe, register
from core.errors import Conflict
from pricing.services.common import can_see_internal
from procurement.models import Batch, BatchStatus
from procurement.serializers.shapes import CommitSerializer
from procurement.services import allocation
from procurement.views.batches import ReverseSerializer


def _user(context):
    return getattr((context or {}).get("request"), "user", None)


def _body(serializer_class, payload) -> dict:
    serializer = serializer_class(data={key: value for key, value in (payload or {}).items() if key != "expected_version"})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    return {"reason": data["reason"], "effective_from": data.get("effective_from")}


def _describe(target, payload) -> str:
    return f"Batch {target.number} — {target.supplier.name}"[:255] if target is not None else "Batch"


# ── commit ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _validate_commit(operation, payload, target, context):
    data = _body(CommitSerializer, payload)
    if target.status != BatchStatus.DRAFT:
        raise Conflict("batch_not_draft", f"Batch {target.number} is {target.status}; only a DRAFT batch is committed.")
    if not (target.invoice_no or "").strip():
        raise Conflict("invoice_reference_missing", "A batch needs the supplier's invoice / PO number before it becomes a price version.", errors={"invoice_no": ["Required to commit."]})
    preview = allocation.preview(target)
    if not preview["can_commit"]:
        raise Conflict("batch_not_committable", "The batch cannot be committed yet.", errors={item["code"]: [item["message"]] for item in preview["blockers"]})
    return data


def _apply_commit(operation, data, target, user, expected_version):
    return allocation.commit(target, user=user, reason=data["reason"], effective_from=data.get("effective_from"), expected_version=expected_version)


def _snapshot_commit(target, payload, context):
    """The prices the commit would set, line by line (landed cost only for someone who may see it)."""
    preview = allocation.preview(target)
    internal = can_see_internal(_user(context))
    lines = [
        {
            "sku": item["component"].sku,
            "name": item["component"].name,
            "qty": item["qty"],
            "unit_purchase_price": item["unit_purchase_price"],
            **({"landed_unit_cost": item["landed_unit_cost"]} if internal else {}),
        }
        for item in preview["lines"]
    ]
    return json_safe({"batch": target.number, "supplier": target.supplier.name, "invoice_no": target.invoice_no, "charges_total": preview["charges_total"], "lines": lines})


# ── reverse ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _validate_reverse(operation, payload, target, context):
    data = _body(ReverseSerializer, payload)
    if target.status != BatchStatus.COMMITTED:
        raise Conflict("batch_not_committed", f"Only COMMITTED batches are reversed; {target.number} is {target.status}.")
    if target.reverses_id is not None:
        raise Conflict("batch_is_reversal", "A reversing batch is not reversed again; commit a new batch instead.")
    if Batch.objects.filter(reverses=target).exists():
        raise Conflict("batch_already_reversed", f"{target.number} has already been reversed.")
    return data


def _apply_reverse(operation, data, target, user, expected_version):
    reversal, _ = allocation.reverse(target, user=user, reason=data["reason"], effective_from=data.get("effective_from"), expected_version=expected_version)
    return reversal


def _snapshot_reverse(target, payload, context):
    lines = [{"sku": line.component.sku, "name": line.component.name, "qty": line.qty, "unit_purchase_price": line.unit_purchase_price} for line in target.lines.select_related("component")]
    return json_safe({"batch": target.number, "supplier": target.supplier.name, "invoice_no": target.invoice_no, "effective_from": target.effective_from, "lines": lines})


def register_kinds() -> None:
    register(
        ChangeKind(
            key="procurement.batch_commit",
            label="Commit purchase batch (sets purchase & landed prices)",
            module="procurement",
            operations={UPDATE: "commit"},
            model=Batch,
            validate=_validate_commit,
            apply=_apply_commit,
            snapshot=_snapshot_commit,
            describe=_describe,
        )
    )
    register(
        ChangeKind(
            key="procurement.batch_reverse",
            label="Reverse purchase batch (restores earlier prices)",
            module="procurement",
            operations={UPDATE: "commit"},
            model=Batch,
            validate=_validate_reverse,
            apply=_apply_reverse,
            snapshot=_snapshot_reverse,
            describe=_describe,
        )
    )
