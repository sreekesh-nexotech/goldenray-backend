"""Runs an import plan (PLAN §7.1): ordered steps, one audited batch per step, dry run, resume.

A :class:`Plan` is an ordered list of :class:`Step` s in FK order (users/roles → media → masters → content →
transactional rows). Each step reads its source tables, calls the owning apps' ``services/legacy_import.py`` functions
and returns their results. The runner:

* commits each step in its own transaction together with its **batch audit row** (``migrations_tools.batch_imported``,
  object ``migrations_tools.importrun`` = the run uid): per table the row count and SHA-256, the importers' counts, the
  number of violations and the source ids they name (PLAN §7.6 #12). A failing step is rolled back alone; the steps
  before it stay committed;
* ``releases`` (PLAN §7.1 "… transactional rows → releases") is the last phase: PriceRelease #1 / PackRelease #1;
* **resumes** a run (``resume=<run uid>``): a step whose batch row exists for that run with the same source checksum is
  not run again; everything else is (the importers are idempotent, so re-running a step is always safe);
* ``dry_run`` runs every step in one transaction that is rolled back at the end, with the media storage replaced by a
  discarding one: counts and violations are exact, nothing is written (not even audit rows or files).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from audit.models import AuditLog
from audit.services import record
from migrations_tools.services.source import Source, checksum

BATCH_ACTION = "migrations_tools.batch_imported"
RUN_OBJECT = "migrations_tools.importrun"
PHASES = ("users", "media", "masters", "content", "transactional", "releases")
MAX_LISTED_IDS = 5000


@dataclass
class Context:
    """What a step gets besides its rows."""

    source_system: str
    user: object = None
    dry_run: bool = False
    read_file: Callable[[str], bytes | None] | None = None
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    name: str
    phase: str
    tables: tuple[str, ...]
    run: Callable[[dict[str, list[dict]], Context], dict[str, dict]]
    description: str = ""


@dataclass(frozen=True)
class Plan:
    source_system: str
    steps: tuple[Step, ...]
    not_migrated: dict[str, str] = field(default_factory=dict)  # source table → reason (PLAN §7.6 #1 "listed")
    # PLAN §7.6 #1 for sources whose rows are not ``{id}`` rows of the mapped table (JSON documents, browser exports):
    # source table → fn(rows) → the ``(core_legacy_map source_table, source_id)`` pairs that must be mapped or listed.
    row_keys: dict[str, Callable[[list[dict]], list[tuple[str, str]]]] = field(default_factory=dict)

    def keys_of(self, table: str, rows: list[dict]) -> list[tuple[str, str]]:
        if table in self.row_keys:
            return self.row_keys[table](rows)
        return [(table, str(row["id"])) for row in rows if "id" in row]

    def __post_init__(self):
        order = [PHASES.index(step.phase) for step in self.steps]
        if order != sorted(order):
            raise ValueError("plan steps must follow the FK order users → media → masters → content → transactional → releases")

    @property
    def tables(self) -> list[str]:
        return list(dict.fromkeys(table for step in self.steps for table in step.tables))


@dataclass
class StepReport:
    step: str
    phase: str
    status: str  # imported | resumed | failed
    tables: dict = field(default_factory=dict)  # table → {rows, checksum}
    results: dict = field(default_factory=dict)  # importer label → normalised result
    checksum: str = ""
    error: str = ""


@dataclass
class RunReport:
    run_uid: str
    source_system: str
    source: str
    dry_run: bool
    steps: list[StepReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def totals(self) -> dict:
        totals = Counter()
        for step in self.steps:
            for result in step.results.values():
                for key in ("created", "updated", "unchanged", "violations"):
                    totals[key] += result[key] if key != "violations" else len(result["violations"])
        return dict(totals)


def normalise(result: dict) -> dict:
    """Any importer's result → ``{created, updated, unchanged, violations}`` (``skipped``/``unchanged`` merged)."""
    violations = result.get("violations") or []
    return {
        "created": int(result.get("created") or 0),
        "updated": int(result.get("updated") or 0),
        "unchanged": int(result.get("skipped") or 0) + int(result.get("unchanged") or 0),
        "violations": list(violations),
    }


def violation_table(violation: dict, default: str | None) -> str | None:
    return violation.get("source_table") or violation.get("table") or default


def listed_ids(results: dict[str, dict], step: Step) -> dict[str, list[str]]:
    """Per source table, the ids the importers' violations name (the rows "listed", PLAN §7.6 #1)."""
    listed: dict[str, set[str]] = {}
    for label, result in results.items():
        default = label if label in step.tables else (step.tables[0] if len(step.tables) == 1 else None)
        for violation in result["violations"]:
            table = violation_table(violation, default)
            source_id = violation.get("source_id")
            if table and source_id not in (None, ""):
                listed.setdefault(str(table), set()).add(str(source_id))
    return {table: sorted(ids)[:MAX_LISTED_IDS] for table, ids in listed.items()}


def completed_steps(run_uid: str) -> dict[str, str]:
    """``{step name: source checksum}`` of the batches recorded for ``run_uid``."""
    rows = AuditLog.objects.filter(action=BATCH_ACTION, object_type=RUN_OBJECT, object_uid=run_uid).values_list("after", flat=True)
    return {after["step"]: after["checksum"] for after in rows if after and after.get("step")}


def latest_run(source_system: str) -> str | None:
    row = AuditLog.objects.filter(action=BATCH_ACTION, object_type=RUN_OBJECT, after__source_system=source_system, after__dry_run=False).order_by("-at", "-id").first()
    return str(row.object_uid) if row else None


def batch_rows(run_uid: str):
    return AuditLog.objects.filter(action=BATCH_ACTION, object_type=RUN_OBJECT, object_uid=run_uid).order_by("at", "id")


class DiscardingStorage:
    """Stand-in for the media storages during a dry run: accepts and forgets every write."""

    name = "dry-run"

    def save(self, key, data):
        return None

    def read(self, key):  # pragma: no cover - nothing is ever stored
        raise FileNotFoundError(key)

    def delete(self, key):
        return None

    def exists(self, key):
        return False

    def url(self, key):
        return f"dry-run://{key}"


@contextmanager
def discarding_storage():
    """Swap ``storage_for`` in the media modules that write files (media uploads, CMS re-uploads) for the run."""
    import media.services.assets as assets
    import media.services.legacy_import as media_import

    storage = DiscardingStorage()
    saved = [(module, module.storage_for) for module in (assets, media_import)]
    try:
        for module, _ in saved:
            module.storage_for = lambda visibility: storage
        yield
    finally:
        for module, original in saved:
            module.storage_for = original


class FileReader:
    """``read_file(path)`` over a legacy media volume (``--media-root``); never leaves the root."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def __call__(self, path: str) -> bytes | None:
        candidate = (self.root / str(path).lstrip("/")).resolve()
        if self.root not in candidate.parents or not candidate.is_file():
            return None
        return candidate.read_bytes()


def run_plan(plan: Plan, source: Source, *, context: Context, resume: str | None = None, only: set[str] | None = None, log: Callable[[str], None] = lambda message: None) -> RunReport:
    run_uid = resume or str(uuid.uuid4())
    done = completed_steps(run_uid) if resume else {}
    report = RunReport(run_uid=run_uid, source_system=plan.source_system, source=source.label, dry_run=context.dry_run)
    with ExitStack() as stack:
        if context.dry_run:
            stack.enter_context(transaction.atomic())
            stack.enter_context(discarding_storage())
        for step in plan.steps:
            if only and step.name not in only:
                continue
            rows = {table: source.rows(table) for table in step.tables}
            tables = {table: {"rows": len(table_rows), "checksum": checksum(table_rows), "present": source.has_table(table)} for table, table_rows in rows.items()}
            step_sum = checksum([{table: meta["checksum"]} for table, meta in tables.items()])
            if done.get(step.name) == step_sum:
                report.steps.append(StepReport(step.name, step.phase, "resumed", tables=tables, checksum=step_sum))
                log(f"{step.name}: already imported by run {run_uid} (same source checksum) — skipped")
                continue
            log(f"{step.name}: {', '.join(f'{table} {meta['rows']}' for table, meta in tables.items())}")
            try:
                with transaction.atomic():
                    results = {label: normalise(result) for label, result in step.run(rows, context).items()}
                    _record_batch(plan, step, run_uid, tables, results, step_sum, context)
            except Exception as exc:  # noqa: BLE001 - reported, the run stops, earlier steps stay committed
                report.steps.append(StepReport(step.name, step.phase, "failed", tables=tables, checksum=step_sum, error=f"{exc.__class__.__name__}: {exc}"))
                log(f"{step.name}: FAILED — {exc.__class__.__name__}: {exc}")
                break
            report.steps.append(StepReport(step.name, step.phase, "imported", tables=tables, results=results, checksum=step_sum))
        if context.dry_run:
            transaction.set_rollback(True)
    return report


def _record_batch(plan: Plan, step: Step, run_uid: str, tables: dict, results: dict, step_sum: str, context: Context) -> None:
    codes = Counter(str(violation.get("code") or violation.get("field") or "violation") for result in results.values() for violation in result["violations"])
    record(
        BATCH_ACTION,
        object_type=RUN_OBJECT,
        object_uid=run_uid,
        actor=context.user,
        actor_kind=None if context.user else "SYSTEM",
        after={
            "source_system": plan.source_system,
            "step": step.name,
            "phase": step.phase,
            "dry_run": context.dry_run,
            "checksum": step_sum,
            "tables": tables,
            "counts": {label: {key: (len(value) if key == "violations" else value) for key, value in result.items()} for label, result in results.items()},
            "violation_codes": dict(codes),
            "listed": listed_ids(results, step),
        },
        note=f"{plan.source_system} import batch {step.name}",
    )
