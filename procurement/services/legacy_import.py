"""Flarize procurement → platform (PLAN §7.4; contract in :mod:`pricing.services.import_support`).

:func:`import_flarize_procurement` takes ``procurement-state.json`` (suppliers, batches, its ``historyStore``), the
standalone ``commercial-history.json`` (the same history, compared record by record) and
``procurement-price-master.json`` (the projection the cost engine reads, verified against the imported rows):

* suppliers → ``procurement_supplier`` (``supplierId`` → ``code``, ``reference`` kept);
* batches → ``procurement_batch`` (``batchId`` → ``number``, ``batchReference`` → ``invoice_no``, ``batchDate`` →
  ``invoice_date``, ``deliveryCost`` → one FREIGHT charge — null means "not entered", so no charge —,
  ``otherChargesDeclared`` → ``other_charges_declared``, ``isSeed``/``seedNote``, ``effectiveFrom``, ``changeReason``,
  ``committedAt``/``committedBy``) with their lines; DRAFT batches stay DRAFT (re-runs replace their lines while they
  are still DRAFT), COMMITTED batches become COMMITTED with the history's allocation on each line;
* every ``PROCUREMENT_PRICE`` history version → a PURCHASE and a LANDED ``pricing_price`` row with the Flarize
  ``versionId`` as ``version_key`` (history order and effective dates kept: a superseded version is closed at its
  successor's ``effectiveFrom``), linked to the batch line that produced it;
* the price master → verification only (PLAN §7.6 #7): every ``landedUnitCost`` / ``purchasePrice`` must equal the
  current LANDED / PURCHASE row (``price_master_differs`` otherwise); a record without history is imported from the
  price master itself.

Idempotent: rows are found through ``core_legacy_map`` / ``version_key``; price rows are append-only, so a history
version that changed since the last run is reported (``history_differs``), never rewritten. The ``audit`` trail of
the Flarize workspace is not migrated (the platform audit log starts at the import; one row per import call).
"""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from core.errors import DomainError
from core.models import actor_or_none
from core.services import stamp_create
from pricing.models import Price, PriceKind, PriceSource
from pricing.services.common import AUTHORING_NAMESPACE
from pricing.services.import_support import FLARIZE, BadValue, ImportRun, component_for, day, dec, guarded, legacy_user, mapped, moment, remember, run, set_timestamps
from pricing.services.prices import close_row, current_row, write_price
from procurement.models import ALLOCATION_METHOD, Batch, BatchCharge, BatchLine, BatchStatus, ChargeKind, Supplier
from procurement.services.batches import recompute_totals

ACTION = "procurement.legacy_import"
STATE = "procurement-state.json"
HISTORY_TYPE = "PROCUREMENT_PRICE"


def import_flarize_procurement(procurement_state: dict, price_master: dict | None = None, commercial_history: dict | None = None, *, user=None, dry_run: bool = False) -> dict:
    procurement_state = procurement_state or {}
    history = _merged_history(procurement_state.get("historyStore") or {}, commercial_history)

    def body(result: ImportRun) -> None:
        for supplier_id, supplier in sorted((procurement_state.get("suppliers") or {}).items()):
            guarded(result, f"{STATE}:suppliers", supplier_id, lambda supplier=supplier, supplier_id=supplier_id: _supplier(supplier_id, supplier, user=user))
        lines_by_version: dict[str, BatchLine] = {}
        batches = procurement_state.get("batches") or {}
        for batch_id, batch in sorted(batches.items()):
            guarded(result, f"{STATE}:batches", batch_id, lambda batch=batch, batch_id=batch_id: _batch(result, batch_id, batch, lines_by_version, user=user))
        for record_key, versions in sorted(history.items()):
            _history_record(result, record_key, versions, lines_by_version, user=user)
        for batch_id, batch in sorted(batches.items()):
            if batch.get("status") == BatchStatus.COMMITTED:
                guarded(result, f"{STATE}:batches", f"{batch_id}:commit", lambda batch=batch, batch_id=batch_id: _commit_batch(result, batch_id, batch, user=user))
        if commercial_history is not None:
            _compare_histories(result, procurement_state.get("historyStore") or {}, commercial_history)
        for component_id, record in sorted((price_master or {}).items()):
            guarded(result, "procurement-price-master.json", component_id, lambda record=record, component_id=component_id: _verify_master(result, component_id, record, user=user))

    return run(
        "Flarize procurement", ACTION, [procurement_state, price_master, commercial_history], body, user=user, dry_run=dry_run, object_type="procurement.batch", namespaces=(AUTHORING_NAMESPACE,)
    )


def _merged_history(state_history: dict, commercial_history: dict | None) -> dict[str, list[dict]]:
    """``{record key: versions}`` of both stores (the commercial history adds records/versions the state lacks)."""
    merged = {key: list(versions) for key, versions in (state_history.get("records") or {}).items() if key.startswith(f"{HISTORY_TYPE}::")}
    for key, versions in ((commercial_history or {}).get("records") or {}).items():
        if not key.startswith(f"{HISTORY_TYPE}::"):
            continue
        known = {version.get("versionId") for version in merged.get(key, [])}
        merged.setdefault(key, []).extend(version for version in versions if version.get("versionId") not in known)
    return merged


def _compare_histories(result: ImportRun, state_history: dict, commercial_history: dict) -> None:
    state = {(key, v.get("versionId")): v for key, versions in (state_history.get("records") or {}).items() for v in versions}
    for key, versions in (commercial_history.get("records") or {}).items():
        for version in versions:
            other = state.get((key, version.get("versionId")))
            if other is not None and other != version:
                result.violation(
                    "commercial-history.json",
                    version.get("versionId"),
                    "history_differs",
                    f"{version.get('versionId')} differs between commercial-history.json and procurement-state.json.",
                    severity="warning",
                )


# ── suppliers and batches ──────────────────────────────────────────────────────────────────────────────────────────


def _supplier(supplier_id: str, data: dict, *, user) -> str:
    values = {"code": supplier_id, "name": str(data.get("name") or supplier_id), "reference": str(data.get("reference") or "")[:64]}
    supplier = mapped(FLARIZE, f"{STATE}:suppliers", supplier_id, Supplier) or Supplier.objects.filter(code__iexact=supplier_id).first()
    if supplier is None:
        supplier = Supplier(**values)
        stamp_create(supplier, legacy_user(FLARIZE, data.get("createdBy")) or user)
        supplier.save()
        created_at = moment(data.get("createdAt"))
        set_timestamps(supplier, created_at=created_at, updated_at=created_at)
        outcome = "created"
    else:
        diff = {name: value for name, value in values.items() if getattr(supplier, name) != value}
        if diff:
            supplier.versioned_update(user, **diff)
        outcome = "updated" if diff else "skipped"
    remember(FLARIZE, f"{STATE}:suppliers", supplier_id, supplier)
    return outcome


def _batch(result: ImportRun, batch_id: str, data: dict, lines_by_version: dict, *, user) -> str:
    if (data.get("allocationMethod") or ALLOCATION_METHOD) != ALLOCATION_METHOD:
        raise BadValue(f"allocationMethod={data.get('allocationMethod')!r}: only {ALLOCATION_METHOD} is approved")
    supplier = mapped(FLARIZE, f"{STATE}:suppliers", data.get("supplierId"), Supplier)
    if supplier is None:
        raise BadValue(f"supplierId={data.get('supplierId')!r}: unknown supplier")
    batch = mapped(FLARIZE, f"{STATE}:batches", batch_id, Batch) or Batch.objects.filter(number=batch_id).first()
    note = "\n".join(part for part in (data.get("seedNote") or "",) if part)
    header = {
        "supplier": supplier,
        "invoice_no": str(data.get("batchReference") or "")[:64],
        "invoice_date": day(data.get("batchDate")),
        "is_seed": bool(data.get("isSeed")),
        "other_charges_declared": dec(data.get("otherChargesDeclared"), "otherChargesDeclared"),
        "note": note,
    }
    creator = legacy_user(FLARIZE, data.get("createdBy")) or user
    if batch is None:
        batch = Batch(number=batch_id, status=BatchStatus.DRAFT, **header)
        stamp_create(batch, creator)
        batch.save()
        created_at = moment(data.get("createdAt"))
        set_timestamps(batch, created_at=created_at, updated_at=created_at)
        outcome = "created"
    elif batch.status != BatchStatus.DRAFT:
        _index_committed_lines(batch, lines_by_version)
        remember(FLARIZE, f"{STATE}:batches", batch_id, batch)
        return "skipped"
    else:
        diff = {name: value for name, value in header.items() if getattr(batch, name) != value}
        if diff:
            batch.versioned_update(user, **diff)
        outcome = "updated" if diff else "skipped"
    _replace_charges(batch, data.get("deliveryCost"), user=creator)
    _replace_lines(result, batch, batch_id, data.get("lines") or [], lines_by_version, user=creator)
    totals = recompute_totals(batch)
    if any(getattr(batch, name) != value for name, value in totals.items()):
        batch.versioned_update(user, **totals)
    remember(FLARIZE, f"{STATE}:batches", batch_id, batch)
    return outcome


def _index_committed_lines(batch: Batch, lines_by_version: dict) -> None:
    for line in BatchLine.objects.filter(batch=batch).select_related("component"):
        lines_by_version[f"{batch.number}::{line.component.sku}"] = line


def _replace_charges(batch: Batch, delivery_cost, *, user) -> None:
    amount = dec(delivery_cost, "deliveryCost")
    existing = list(BatchCharge.objects.filter(batch=batch))
    wanted = [] if amount is None else [(ChargeKind.FREIGHT, amount)]
    if [(charge.kind, charge.amount) for charge in existing] == wanted:
        return
    for charge in existing:
        charge.soft_delete(user)
    for kind, value in wanted:
        charge = BatchCharge(batch=batch, kind=kind, amount=value, note="Flarize deliveryCost (supplier → warehouse)")
        stamp_create(charge, user)
        charge.save()


def _replace_lines(result: ImportRun, batch: Batch, batch_id: str, lines: list[dict], lines_by_version: dict, *, user) -> None:
    table = f"{STATE}:batches.lines"
    existing = {line.component_id: line for line in BatchLine.objects.filter(batch=batch)}
    seen = set()
    for item in lines:
        component_id = item.get("componentId")
        source_id = f"{batch_id}:{component_id}"
        component = component_for(FLARIZE, "catalog.json:items", component_id, component_id)
        if component is None:
            result.violation(table, source_id, "component_not_found", f"No catalog component {component_id!r}; line not imported.")
            continue
        if item.get("otherProcurementCharges"):
            result.violation(table, source_id, "line_charges_not_supported", "Line-level other procurement charges have no home; enter them as batch charges.")
        try:
            qty = dec(item.get("quantity"), "quantity", places=3, digits=12)
            price = dec(item.get("purchaseUnitPrice"), "purchaseUnitPrice")
        except BadValue as exc:
            result.violation(table, source_id, "invalid_value", str(exc))
            continue
        if qty is None or qty <= 0 or price is None or price < 0:
            result.violation(table, source_id, "invalid_value", "A line needs a positive quantity and a purchase price ≥ 0.")
            continue
        seen.add(component.pk)
        line = existing.get(component.pk)
        if line is None:
            line = BatchLine(batch=batch, component=component, qty=qty, unit_purchase_price=price)
            stamp_create(line, user)
            line.save()
        elif line.qty != qty or line.unit_purchase_price != price:
            line.versioned_update(user, qty=qty, unit_purchase_price=price)
        remember(FLARIZE, table, source_id, line)
        lines_by_version[f"{batch_id}::{component_id}"] = line
    for component_pk, line in existing.items():
        if component_pk not in seen:
            line.soft_delete(user)


def _commit_batch(result: ImportRun, batch_id: str, data: dict, *, user) -> str:
    batch = mapped(FLARIZE, f"{STATE}:batches", batch_id, Batch)
    if batch is None:
        return "skipped"
    if batch.status == BatchStatus.COMMITTED:
        return None
    missing = [line.component.sku for line in BatchLine.objects.filter(batch=batch).select_related("component") if line.price_row_id is None or line.landed_row_id is None]
    if missing:
        result.violation(f"{STATE}:batches", batch_id, "line_without_version", f"{len(missing)} lines have no committed price version in the history.", severity="warning", skus=missing[:50])
    committed_at = moment(data.get("committedAt")) or timezone.now()
    effective_from = day(data.get("effectiveFrom")) or committed_at.date()
    batch.versioned_update(
        user,
        status=BatchStatus.COMMITTED,
        committed_at=committed_at,
        committed_by=actor_or_none(legacy_user(FLARIZE, data.get("committedBy"))),
        effective_from=effective_from,
        commit_reason=data.get("changeReason") or "",
        **recompute_totals(batch),
    )
    return None


# ── history → price rows ───────────────────────────────────────────────────────────────────────────────────────────


def _history_record(result: ImportRun, record_key: str, versions: list[dict], lines_by_version: dict, *, user) -> None:
    component_id = record_key.split("::", 1)[1]
    table = "commercial-history"
    component = component_for(FLARIZE, "catalog.json:items", component_id, component_id)
    if component is None:
        result.violation(table, record_key, "component_not_found", f"No catalog component {component_id!r}; its price history was not imported.")
        result.count(table, "skipped")
        return
    for index, version in enumerate(versions):
        successor = versions[index + 1] if index + 1 < len(versions) else None
        guarded(result, table, version.get("versionId"), lambda version=version, successor=successor: _history_version(result, component, version, successor, lines_by_version, user=user))


def _history_version(result: ImportRun, component, version: dict, successor: dict | None, lines_by_version: dict, *, user) -> str:
    version_id = str(version.get("versionId") or "")
    value = version.get("value") or {}
    if not version_id:
        raise BadValue("versionId: missing")
    purchase_amount = dec(value.get("purchasePrice"), "purchasePrice")
    landed_amount = dec(value.get("landedUnitCost"), "landedUnitCost")
    effective_from = day(version.get("effectiveFrom"))
    if effective_from is None:
        raise BadValue(f"{version_id}: effectiveFrom missing")
    line = lines_by_version.get(version_id)
    supplier = mapped(FLARIZE, f"{STATE}:suppliers", value.get("supplierId"), Supplier)
    creator = legacy_user(FLARIZE, version.get("changedBy")) or user
    created_at = moment(version.get("changedAt"))
    outcome = "skipped"
    rows = {}
    for kind, amount in ((PriceKind.PURCHASE, purchase_amount), (PriceKind.LANDED, landed_amount)):
        if amount is None:
            continue
        existing = Price.all_objects.filter(kind=kind, version_key=version_id).first()
        if existing is not None:
            if existing.amount != amount or existing.component_id != component.pk:
                result.violation(
                    "commercial-history",
                    version_id,
                    "history_differs",
                    f"{version_id} {kind}: imported {existing.amount}, the source now says {amount} (price rows are never rewritten).",
                    severity="warning",
                )
            rows[kind] = existing
            continue
        try:
            row, _ = write_price(
                component,
                kind,
                amount,
                user=creator,
                source=PriceSource.BATCH,
                effective_from=effective_from,
                source_ref=str(line.uid) if line is not None else f"FLARIZE:{version_id}"[:64],
                supplier=supplier,
                note=f"Landed = {value.get('formula')}" if kind == PriceKind.LANDED and value.get("formula") else (version.get("changeReason") or ""),
                version_key=version_id,
                created_at=created_at,
            )
        except DomainError as exc:
            result.violation("commercial-history", version_id, exc.code, f"{version_id} {kind}: {exc.message}")
            continue
        remember(FLARIZE, f"commercial-history:{kind}", version_id, row)
        rows[kind] = row
        outcome = "created"
    _close_if_superseded(version, successor, rows, user=user)
    if line is not None and rows:
        links = {"price_row": rows.get(PriceKind.PURCHASE), "landed_row": rows.get(PriceKind.LANDED)}
        values = {name: row for name, row in links.items() if row is not None and getattr(line, f"{name}_id") != row.pk}
        allocation = {
            "landed_unit_cost": landed_amount,
            "allocated_charges": dec(value.get("allocatedDeliveryCost"), "allocatedDeliveryCost"),
            "allocation_pct": dec(value.get("allocationPct"), "allocationPct", places=4, digits=9),
        }
        values.update({name: amount for name, amount in allocation.items() if getattr(line, name) != amount})
        if values:
            line.versioned_update(user, **values)
    return outcome


def _close_if_superseded(version: dict, successor: dict | None, rows: dict, *, user) -> None:
    """A version marked SUPERSEDED/ARCHIVED with no successor in the import is closed at its own ``effectiveTo``."""
    if successor is not None or version.get("status") == "ACTIVE":
        return
    closing = day(version.get("effectiveTo")) or day(version.get("effectiveFrom"))
    for row in rows.values():
        if row.effective_to is None and current_row(row.component, row.kind) == row:
            close_row(row, closing, user=user)


def _verify_master(result: ImportRun, component_id: str, record: dict, *, user) -> str:
    table = "procurement-price-master.json"
    component = component_for(FLARIZE, "catalog.json:items", component_id, component_id)
    if component is None:
        result.violation(table, component_id, "component_not_found", f"No catalog component {component_id!r}.")
        return "skipped"
    outcome = "skipped"
    for kind, amount_field, version_field, from_field in (
        (PriceKind.PURCHASE, "purchasePrice", "purchasePriceVersion", "purchasePriceEffectiveFrom"),
        (PriceKind.LANDED, "landedUnitCost", "landedCostVersion", "landedCostEffectiveFrom"),
    ):
        amount = dec(record.get(amount_field), amount_field)
        if amount is None:
            continue
        current = current_row(component, kind)
        if current is None:
            row, _ = write_price(
                component,
                kind,
                amount,
                user=user,
                source=PriceSource.IMPORT,
                effective_from=day(record.get(from_field)),
                source_ref=f"FLARIZE:{table}#{component_id}"[:64],
                note=record.get("notes") or "",
                version_key=str(record.get(version_field) or ""),
            )
            remember(FLARIZE, f"{table}:{kind}", component_id, row)
            result.violation(table, component_id, "master_without_history", f"{component_id} {kind}: no history version; imported from the price master.", severity="warning")
            outcome = "created"
        elif current.amount != amount or (record.get(version_field) and current.version_key != record.get(version_field)):
            result.violation(
                table,
                component_id,
                "price_master_differs",
                f"{component_id} {kind}: the price master says {amount} ({record.get(version_field)}), the current row is {current.amount} ({current.version_key}).",
                severity="warning",
                master=str(amount),
                current=str(current.amount),
            )
    return outcome


def committed_landed_costs() -> dict[str, Decimal]:
    """``{sku: current LANDED amount}`` — what §7.6 #7 compares with the price master."""
    return {row.component.sku: row.amount for row in Price.objects.filter(kind=PriceKind.LANDED, effective_to__isnull=True).select_related("component")}
