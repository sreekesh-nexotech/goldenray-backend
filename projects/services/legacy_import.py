"""Import of Flarize ``data/workspace-state.json`` and ``data/bom-state.json`` (PLAN §7.4) — called by ``migrations_tools``.

Contract (same as :mod:`customers.services.import_support`): plain row dicts in, ``{"created", "updated", "skipped",
"violations"}`` out, idempotent through ``core_legacy_map`` (``FLARIZE`` / ``workspace_projects`` / ``projectId``):
re-running updates the mapped project, never duplicates it. ``dry_run=True`` rolls everything back.

Rules (:func:`import_flarize_workspace_projects`, rows = ``workspace-state.json`` ``projects`` values):

* only projects whose BOM is **LOCKED** (``lock`` present) become ``projects_project`` rows; an open (DRAFT) workspace is
  not migrated and is reported (``open_workspace_not_migrated``, counted as skipped);
* customer: ``customer.customerId`` through the imported customers (``FLARIZE`` / ``customers``; a merged customer is
  followed to the survivor), else the customer with
  that ``code``, else the live customer with the E.164 ``customer.phone``; none → ``customer_not_found`` (skipped);
* ``sysType`` ongrid/hybrid → ``ON_GRID``/``HYBRID`` (anything else ``UNDECIDED``, ``unknown_system_type``), ``tier`` →
  upper case, ``sizeKw`` → ``size_kw``, ``phase`` kept, ``packageId`` → ``title``;
* status IN_PROGRESS (the BOM is locked; PLAN lifecycle), ``bom_locked_at`` = ``lock.lockedAt``, ``bom_locked_by`` =
  ``lock.lockedBy`` through the imported users (``FLARIZE`` / ``users``; unmapped → empty, ``unmapped_user``);
* ``bom_lock`` = the lock snapshot in the platform shape with ``legacy: true``: each line's ``componentId`` is the SKU
  (``component_uid`` resolved from the catalog, else null with ``unknown_component``), ``unitSellingPrice`` /
  ``unitPurchaseCost`` → ``unit_list_price`` / ``unit_landed_cost``; the engineering verdict (``engineeringStatus``
  VALID/WARNING/BLOCKED → PASS/WARN/FAIL, ``rulesVersion``, ``lastValidation.counts``) and the acknowledgements are kept
  (no ``engineering_run`` row is created: the historic verdict lives in the snapshot, ``run_uid`` null);
* ``costInputs`` → ``cost_inputs`` (snake_case keys, ``installationType`` upper-cased); ``createdAt``/``createdBy`` preserved.

:func:`report_flarize_bom_state` — ``bom-state.json`` is the legacy React BOM tool's UI state (not a project): it is not
migrated; the report says so.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from accounts.models import User
from catalog.models import Component
from core.models import LegacyMap
from customers.models import Customer
from customers.services.customers import find_by_phone
from customers.services.import_support import ImportRun, mapped_id, run_import, timestamp, upsert
from customers.services.phones import try_normalise
from projects.models import Phase, Project, ProjectStatus, SystemType
from projects.services.bom_lock import SNAPSHOT_SCHEMA
from projects.services.projects import CACHE_NAMESPACE, next_project_number

FLARIZE = LegacyMap.SourceSystem.FLARIZE
SOURCE_TABLE = "workspace_projects"
ACTION = "projects.legacy_import"
SYS_TYPES = {"ongrid": SystemType.ON_GRID, "hybrid": SystemType.HYBRID, "offgrid": SystemType.OFF_GRID}
RESULTS = {"VALID": "PASS", "WARNING": "WARN", "BLOCKED": "FAIL"}
COST_KEYS = {
    "distanceKm": "distance_km",
    "vehicleType": "vehicle_type",
    "installationType": "installation_type",
    "structureMaterialCost": "structure_material_cost",
    "specialWorks": "special_works",
    "rateEffectiveAt": "rate_effective_at",
    "sizeKey": "size_key",
}


def _text(value, limit: int) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def _user(run: ImportRun, row_id: str, legacy_user, column: str) -> User | None:
    if legacy_user in (None, "", "SYSTEM"):
        return None
    target = mapped_id(FLARIZE, "users", legacy_user)
    if target is None:
        run.violation(row_id, "unmapped_user", f"{column}={legacy_user!r} has no imported user; left empty.")
        return None
    return User.objects.filter(pk=target).first()


def _survivor(pk) -> Customer | None:
    """The live customer ``pk`` is, or was merged into (a merge soft-deletes the loser and records ``merged_into``)."""
    found = Customer.all_objects.filter(pk=pk).first() if pk is not None else None
    for _ in range(10):
        if found is None or found.deleted_at is None or found.merged_into_id is None:
            break
        found = Customer.all_objects.filter(pk=found.merged_into_id).first()
    return found if found is not None and found.deleted_at is None else None


def _customer(row: dict) -> Customer | None:
    source = row.get("customer") or {}
    legacy_id = _text(source.get("customerId"), 128)
    if legacy_id:
        found = _survivor(mapped_id(FLARIZE, "customers", legacy_id))
        found = found or Customer.objects.filter(code=legacy_id[:24]).first()
        if found is not None:
            return found
    phone = try_normalise(_text(source.get("phone"), 32)) if source.get("phone") else None
    return find_by_phone(phone) if phone else None


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _snapshot(run: ImportRun, row_id: str, row: dict, lock: dict) -> dict:
    skus = [line.get("componentId") for line in lock.get("lines") or []]
    components = {c.sku: c for c in Component.all_objects.filter(sku__in=[s for s in skus if s])}
    lines = []
    for line in lock.get("lines") or []:
        sku = _text(line.get("componentId"), 64)
        component = components.get(sku)
        if component is None:
            run.violation(row_id, "unknown_component", f"lock line componentId={sku!r} is not in the catalog; kept by SKU only.")
        lines.append(
            {
                "component_uid": str(component.uid) if component else None,
                "sku": sku,
                "name": component.name if component else "",
                "role": _text(line.get("role"), 32),
                "quantity": str(_decimal(line.get("quantity")) if _decimal(line.get("quantity")) is not None else ""),
                "selection_method": _text(line.get("selectionMethod"), 32) or "PACKAGE_DEFAULT",
                "reason": _text(line.get("reason"), 2000),
                "unit_list_price": None if line.get("unitSellingPrice") is None else str(line["unitSellingPrice"]),
                "unit_landed_cost": None if line.get("unitPurchaseCost") is None else str(line["unitPurchaseCost"]),
            }
        )
    acknowledgements = []
    for ack in lock.get("acknowledgements") or row.get("acknowledgements") or []:
        by = mapped_id(FLARIZE, "users", ack.get("acknowledgedBy")) if ack.get("acknowledgedBy") else None
        by_uid = User.objects.filter(pk=by).values_list("uid", flat=True).first() if by else None
        acknowledgements.append(
            {
                "rule_code": _text(ack.get("ruleId"), 16),
                "severity": "WARN",
                "identity": "",
                "reason": _text(ack.get("reason"), 2000),
                "acknowledged_by": str(by_uid) if by_uid else None,
                "acknowledged_at": ack.get("acknowledgedAt"),
                "waiver": False,
                "legacy_actor": ack.get("acknowledgedBy"),
            }
        )
    validation = row.get("lastValidation") or {}
    status = _text(lock.get("engineeringStatus") or lock.get("validationStatus"), 16)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "status": "LOCKED",
        "locked_at": lock.get("lockedAt"),
        "locked_by": None,
        "architecture": _text(lock.get("architecture") or row.get("architecture"), 16),
        "system_type": SYS_TYPES.get(_text(row.get("sysType"), 16).lower(), SystemType.UNDECIDED).value,
        "phase": _text(row.get("phase"), 4),
        "price_release_number": None,
        "engineering": {"run_uid": None, "result": RESULTS.get(status), "rules_version": lock.get("rulesVersion"), "counts": validation.get("counts")},
        "lines": lines,
        "acknowledgements": acknowledgements,
        "legacy": True,
        "source": {"project_id": row_id, "package_id": lock.get("packageId") or row.get("packageId"), "data_version": lock.get("dataVersion"), "locked_by": lock.get("lockedBy")},
    }


def _cost_inputs(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    inputs = {COST_KEYS.get(key, key): value for key, value in raw.items()}
    if isinstance(inputs.get("installation_type"), str):  # Flarize also stored "flat"; the platform value is FLAT
        inputs["installation_type"] = inputs["installation_type"].strip().upper()
    return inputs


def _import_project(run: ImportRun, row: dict) -> None:
    row_id = _text(row.get("projectId"), 128)
    if not row_id:
        run.violation("?", "incomplete_row", "projectId is required; row skipped.")
        run.skipped += 1
        return
    lock = row.get("lock")
    if not lock or (row.get("bom") or {}).get("status", "LOCKED") != "LOCKED":
        run.violation(row_id, "open_workspace_not_migrated", "The BOM was never locked (open workspace); not migrated (PLAN §7.4).")
        run.skipped += 1
        return
    customer = _customer(row)
    if customer is None:
        run.violation(row_id, "customer_not_found", "No imported customer matches customerId or phone; row skipped (import customers.json first).")
        run.skipped += 1
        return
    system_type = SYS_TYPES.get(_text(row.get("sysType"), 16).lower())
    if system_type is None:
        run.violation(row_id, "unknown_system_type", f"sysType={row.get('sysType')!r} imported as UNDECIDED.")
        system_type = SystemType.UNDECIDED
    phase = _text(row.get("phase"), 4).upper()
    if phase and phase not in Phase.values:
        run.violation(row_id, "unknown_phase", f"phase={phase!r} left empty.")
        phase = ""
    size = _decimal(row.get("sizeKw"))
    if size is not None and size <= 0:
        run.violation(row_id, "invalid_size", f"sizeKw={row.get('sizeKw')!r} left empty.")
        size = None
    locked_by = _user(run, row_id, lock.get("lockedBy"), "lock.lockedBy")
    created_by = _user(run, row_id, row.get("createdBy"), "createdBy")
    created_at = timestamp(row.get("createdAt"))
    locked_at = timestamp(lock.get("lockedAt")) or created_at
    snapshot = _snapshot(run, row_id, row, lock)
    snapshot["locked_by"] = str(locked_by.uid) if locked_by else None
    values = {
        "customer_id": customer.pk,
        "title": _text(row.get("packageId"), 160),
        "status": ProjectStatus.IN_PROGRESS,
        "system_type": system_type,
        "tier": _text(row.get("tier"), 10).upper(),
        "size_kw": size.quantize(Decimal("0.01")) if size is not None else None,
        "phase": phase,
        "bom_lock": snapshot,
        "bom_locked_at": locked_at,
        "bom_locked_by_id": locked_by.pk if locked_by else None,
        "cost_inputs": _cost_inputs(row.get("costInputs")),
    }
    target = run.find_target(Project, row_id)
    if target is None:
        values["number"] = next_project_number()
    elif target.status != ProjectStatus.IN_PROGRESS:
        # The platform moved on (commissioned, closed, cancelled): the import never rewinds the lifecycle.
        values.pop("status")
    upsert(run, Project, row_id, target=target, values=values, created_at=created_at, updated_at=locked_at, created_by_id=created_by.pk if created_by else None)


def import_flarize_workspace_projects(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    """Import ``workspace-state.json`` project rows (``list(state["projects"].values())``)."""
    return run_import(ImportRun(FLARIZE, SOURCE_TABLE), rows, _import_project, user=user, dry_run=dry_run, action=ACTION, object_type="projects.project", namespaces=(CACHE_NAMESPACE,))


def report_flarize_bom_state(state: dict | None) -> dict:
    """``bom-state.json``: the legacy React BOM tool's UI state — not migrated (PLAN §7.4), reported."""
    violations = []
    if state:
        violations.append(
            {
                "source_table": "bom_state",
                "source_id": str((state or {}).get("savedAt") or "bom-state.json"),
                "code": "ui_state_not_migrated",
                "message": "bom-state.json holds the legacy BOM tool's UI selections, not a project or a locked BOM; nothing is migrated.",
            }
        )
    return {"created": 0, "updated": 0, "skipped": 1 if state else 0, "violations": violations}
