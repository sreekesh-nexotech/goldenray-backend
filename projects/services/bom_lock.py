"""``projects/<uid>/lock-bom/`` — Flarize ``bomLock.attemptLock`` on the platform (workflows spec C.5).

1. the BOM lines (``component_uid``, ``role``, ``quantity``) are checked by ``engines.engineering_checker`` with the
   ACTIVE engineeringChecker rule set, on the Flarize-shaped catalog priced by the current PriceRelease
   (``packs.services.context.engine_context``);
2. the verdict is stored as an ``engineering_run`` (subject ``PROJECT_BOM``, the project uid) **whether or not the
   lock succeeds**, so engineering can review and waive BLOCK findings under ``engineering/findings/<uid>/acknowledge/``;
3. the lock is refused (409 ``bom_lock_refused``) while a BLOCK finding has no waiver, or a WARN finding whose rule
   requires an acknowledgement (PBC-D-001, PBC-D-003, PBC-F-002, PBC-G-002, PBC-N-001, PBC-R-002 in ``phase1e.1``) has
   neither an acknowledgement in the request (``{rule_code, reason}``) nor one carried from an earlier run (same
   identity **and** message — engineering's ``runs.acknowledged_keys``/``finding_key``);
4. otherwise the request's acknowledgements are recorded as ``engineering_acknowledgement`` rows (they then carry over
   to later runs), the snapshot is frozen into ``bom_lock`` — lines with the PriceRelease list price and landed cost,
   the engineering verdict and every acknowledgement — and the project moves PLANNED → IN_PROGRESS.

A locked BOM is never unlocked (Flarize had no unlock); later price changes never touch the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.services import record
from catalog.models import Component
from core.errors import Conflict, DomainError
from core.services import check_version
from engineering.models import Engine, Finding, Run, SubjectType
from engineering.services import rule_sets, runs
from engines.engineering_checker import check_project_bom
from flarize.cache_utils import bump
from packs.services.context import engine_context
from projects.models import Project, ProjectStatus, SystemType
from projects.services.projects import CACHE_NAMESPACE, emit_project_event

SNAPSHOT_SCHEMA = "projects.bom_lock/1"
#: Flarize ``sysType`` of the platform system type (the checker's rule inputs).
SYS_TYPES = {SystemType.ON_GRID: "ongrid", SystemType.HYBRID: "hybrid", SystemType.OFF_GRID: "offgrid"}


@dataclass(frozen=True)
class Line:
    component: Component
    role: str
    quantity: object
    selection_method: str
    reason: str


def checker_context():
    """The Flarize-shaped catalog (+ battery master) priced by the current PriceRelease; a seam for tests."""
    return engine_context()


def _resolve_lines(raw_lines: list[dict]) -> list[Line]:
    uids = [str(line["component_uid"]) for line in raw_lines]
    components = {str(c.uid): c for c in Component.objects.filter(uid__in=uids)}
    errors = {f"lines[{index}].component_uid": ["No such component."] for index, uid in enumerate(uids) if uid not in components}
    if errors:
        raise DomainError("validation_error", "Some BOM lines name unknown components.", errors=errors)
    return [Line(components[uid], line["role"], line["quantity"], line.get("selection_method") or "PACKAGE_DEFAULT", line.get("reason") or "") for uid, line in zip(uids, raw_lines)]


def _check(project: Project, lines: list[Line], *, architecture: str, rule_set_row, context, at: str):
    engine_rules = rule_sets.engine_rule_set(rule_set_row)
    release = context.price_release
    return (
        check_project_bom(
            bom={"architecture": architecture, "phase": project.phase or None, "sysType": SYS_TYPES.get(project.system_type)},
            catalog=context.catalog,
            battery_master=context.battery_master,
            at=at,
            catalog_version=f"price-release#{release.number}" if release is not None else None,
            lines=[{"role": line.role, "componentId": line.component.sku, "quantity": float(line.quantity)} for line in lines],
            rule_set=engine_rules,
        ),
        engine_rules,
    )


@transaction.atomic
def _record(project: Project, result, *, rule_set_row, user) -> Run:
    return runs.record_run(rule_set=rule_set_row, subject_type=SubjectType.PROJECT_BOM, subject_uid=project.uid, subject_label=project.number, checks=[("", result)], user=user)


def _prices(context, sku: str) -> tuple[str | None, str | None]:
    release = context.price_release
    item = ((release.payload or {}).get("components") or {}).get(sku) if release is not None else None
    if not item:
        return None, None
    return item.get("list_price"), item.get("landed_cost")


def _key(finding: Finding) -> tuple[str, str]:
    """What an earlier acknowledgement must match to carry over: engineering's shared carry-over key, the identity
    (rule + components) **and** the message (:func:`engineering.services.runs.finding_key`) — component-less findings
    share an identity (``|PBC-K-001|`` is every missing role), so a waiver covers only the finding it reviewed."""
    return runs.finding_key(finding.identity, finding.message)


def _acknowledged(project_uid):
    return Finding.objects.filter(run__subject_type=SubjectType.PROJECT_BOM, run__subject_uid=project_uid, deleted_at__isnull=True, acknowledgements__isnull=False)


def _finding_row(finding: Finding) -> dict:
    return {"finding_uid": str(finding.uid), "rule_code": finding.rule_code, "severity": finding.severity, "message": finding.message, "identity": finding.identity}


def lock_bom(project: Project, *, user, architecture: str, lines: list[dict], acknowledgements: list[dict] | None = None, expected_version=None) -> Project:
    """Check, record the run (committed either way), then lock or refuse (409 ``bom_lock_refused``)."""
    check_version(project, expected_version)
    if project.bom_lock is not None:
        raise Conflict("bom_already_locked", "The BOM of this project is already locked.")
    if project.status != ProjectStatus.PLANNED:
        raise Conflict("invalid_transition", f"The BOM of a {project.get_status_display().lower()} project cannot be locked.", errors={"status": [project.status]})
    resolved = _resolve_lines(lines)
    rule_set_row = rule_sets.active_rule_set(Engine.CHECKER)
    context = checker_context()
    now = timezone.now()
    result, engine_rules = _check(project, resolved, architecture=architecture, rule_set_row=rule_set_row, context=context, at=now.isoformat())
    run = _record(project, result, rule_set_row=rule_set_row, user=user)
    locked, refusal = _apply(
        project, run, resolved, engine_rules=engine_rules, context=context, architecture=architecture, acknowledgements=acknowledgements or [], user=user, expected_version=expected_version
    )
    if refusal is not None:
        raise Conflict(
            "bom_lock_refused",
            "The BOM cannot be locked: engineering must waive the blocking findings and every warning that needs one must be acknowledged.",
            errors=refusal,
        )
    return locked


@transaction.atomic
def _apply(project: Project, run: Run, lines: list[Line], *, engine_rules, context, architecture: str, acknowledgements: list[dict], user, expected_version) -> tuple[Project | None, dict | None]:
    locked = Project.objects.select_for_update().select_related("customer").get(pk=project.pk)
    check_version(locked, expected_version)
    if locked.bom_lock is not None or locked.status != ProjectStatus.PLANNED:
        raise Conflict("bom_already_locked", "The BOM of this project is already locked.")
    carried = runs.acknowledged_keys(SubjectType.PROJECT_BOM, locked.uid)
    given = {}
    for item in acknowledgements:
        given.setdefault(item["rule_code"], item["reason"].strip())
    findings = list(Finding.objects.filter(run=run).order_by("sort_order", "id"))
    blocked, missing, to_acknowledge = [], [], []
    for finding in findings:
        if _key(finding) in carried:
            continue
        if finding.severity == "BLOCK":
            blocked.append(_finding_row(finding))
        elif finding.severity == "WARN" and engine_rules.rule(finding.rule_code).requires_acknowledgement:
            if given.get(finding.rule_code):
                to_acknowledge.append(finding)
            else:
                missing.append(_finding_row(finding))
    if blocked or missing:
        errors = {"engineering_run": [str(run.uid)]}
        if blocked:
            errors["blocked"] = [f"{row['rule_code']}: {row['message']} ({row['finding_uid']})" for row in blocked]
        if missing:
            errors["missing_acknowledgements"] = [f"{row['rule_code']}: {row['message']} ({row['finding_uid']})" for row in missing]
        record("projects.bom_lock_refused", obj=locked, actor=user, after={"run": str(run.uid), "result": run.result, "blocked": blocked, "missing_acknowledgements": missing})
        return None, errors  # committed with the audit row; the caller raises bom_lock_refused
    for finding in to_acknowledge:
        runs.acknowledge(finding, user=user, reason=given[finding.rule_code])
    now = timezone.now()
    acknowledged = _acknowledged(locked.uid).select_related("run").prefetch_related("acknowledgements__acknowledged_by").order_by("run__created_at", "sort_order", "id")
    current = {_key(finding) for finding in findings}
    ack_rows, seen = [], set()
    for finding in acknowledged:
        if _key(finding) not in current or _key(finding) in seen:
            continue
        seen.add(_key(finding))
        ack = finding.acknowledgements.all()[0]
        ack_rows.append(
            {
                "rule_code": finding.rule_code,
                "severity": finding.severity,
                "identity": finding.identity,
                "reason": ack.reason,
                "acknowledged_by": str(ack.acknowledged_by.uid) if ack.acknowledged_by else None,
                "acknowledged_at": ack.at.isoformat(),
                "waiver": finding.severity == "BLOCK",
            }
        )
    snapshot_lines = []
    for line in lines:
        list_price, landed_cost = _prices(context, line.component.sku)
        snapshot_lines.append(
            {
                "component_uid": str(line.component.uid),
                "sku": line.component.sku,
                "name": line.component.name,
                "role": line.role,
                "quantity": str(line.quantity),
                "selection_method": line.selection_method,
                "reason": line.reason,
                "unit_list_price": list_price,
                "unit_landed_cost": landed_cost,
            }
        )
    release = context.price_release
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "status": "LOCKED",
        "locked_at": now.isoformat(),
        "locked_by": str(user.uid) if getattr(user, "uid", None) else None,
        "architecture": architecture,
        "system_type": locked.system_type,
        "phase": locked.phase,
        "price_release_number": release.number if release is not None else None,
        "engineering": {
            "run_uid": str(run.uid),
            "result": run.result,
            "rules_version": (run.summary or {}).get("rulesVersion"),
            "counts": (run.summary or {}).get("counts"),
        },
        "lines": snapshot_lines,
        "acknowledgements": ack_rows,
        "legacy": False,
    }
    locked.versioned_update(user, status=ProjectStatus.IN_PROGRESS, bom_lock=snapshot, bom_locked_at=now, bom_locked_by=user if getattr(user, "pk", None) else None, engineering_run=run)
    record("projects.bom_locked", obj=locked, actor=user, after={"run": str(run.uid), "result": run.result, "lines": len(snapshot_lines), "acknowledgements": len(ack_rows)})
    emit_project_event("bom_locked", locked, engineering_run_uid=str(run.uid), engineering_result=run.result)
    bump(CACHE_NAMESPACE)
    return locked, None
