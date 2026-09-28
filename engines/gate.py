"""The quotation generation gate: eight checks that must all PASS before a quotation version may be issued.

Port of ``quotationPayload.evaluateGenerationGate`` (contract §28; workflows spec C.1) with the two pieces of
``quotationWorkspace.js`` that feed it: ``deriveSubsidyTreatment`` (the treatment the gate reads) and
``resolveInputs`` (how a quotation record and its pinned snapshots become gate inputs).

1. ``CUSTOMER_DATA`` — ``customer.customerId`` and ``customer.customerName``.
2. ``SYSTEM_CONFIGURATION`` — ``systemType``, ``systemSizeKw`` (not null) and ``phase``.
3. ``BOM_EXISTS`` — the BOM snapshot has at least one line.
4. ``BOM_LOCKED`` — the BOM snapshot is ``LOCKED``.
5. ``ENGINEERING_VALIDATION`` — an engineering verdict exists and is not ``BLOCKED``.
6. ``COMMERCIAL_SNAPSHOT`` — the commercial snapshot is ``ISSUED``.
7. ``SUBSIDY_RESOLVED`` — the subsidy treatment is ``NOT_QUOTED`` or ``ENGINE_RESULT``.
8. ``QUOTATION_METADATA`` — ``quotationNumber``, ``quotationVersion`` (not null) and ``quotationDate``.

The evaluation reports every check with its reason and enforces nothing; :meth:`GateReport.raise_if_blocked` is the
issue path's hard refusal (``GENERATION_BLOCKED``: nothing may be written). ``GateReport.as_dict()`` is
``quotations_version.gate_report`` and the payload's ``generationGate`` section, key for key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from engines.frozen import deep_freeze
from engines.jscompat import coalesce, is_nullish, js_truthy, prop

__all__ = [
    "GateCheck",
    "GateStatus",
    "SubsidyTreatment",
    "GATE_CHECKS",
    "GATE_REASONS",
    "CheckOutcome",
    "GateReport",
    "GenerationBlocked",
    "evaluate_generation_gate",
    "derive_subsidy_treatment",
    "resolve_inputs",
]


class GateCheck(StrEnum):
    CUSTOMER_DATA = "CUSTOMER_DATA"
    SYSTEM_CONFIGURATION = "SYSTEM_CONFIGURATION"
    BOM_EXISTS = "BOM_EXISTS"
    BOM_LOCKED = "BOM_LOCKED"
    ENGINEERING_VALIDATION = "ENGINEERING_VALIDATION"
    COMMERCIAL_SNAPSHOT = "COMMERCIAL_SNAPSHOT"
    SUBSIDY_RESOLVED = "SUBSIDY_RESOLVED"
    QUOTATION_METADATA = "QUOTATION_METADATA"


class GateStatus(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


class SubsidyTreatment(StrEnum):
    """No subsidy is ever assumed: the treatment is stated (NOT_QUOTED) or comes from the engine (ENGINE_RESULT)."""

    NOT_QUOTED = "NOT_QUOTED"
    ENGINE_RESULT = "ENGINE_RESULT"


#: The eight checks, in evaluation (and report) order.
GATE_CHECKS = tuple(GateCheck)

#: Why each check blocks (the ENGINEERING_VALIDATION reason depends on whether a BLOCKED verdict exists).
GATE_REASONS: Mapping[str, str] = deep_freeze(
    {
        "CUSTOMER_DATA": "Customer record incomplete — customerId and customerName are required.",
        "SYSTEM_CONFIGURATION": "System configuration incomplete — systemType, systemSizeKw and phase are required.",
        "BOM_EXISTS": "No BOM lines on the supplied snapshot.",
        "BOM_LOCKED": "BOM is not LOCKED. A quotation may only be built from a locked engineering configuration.",
        "ENGINEERING_VALIDATION_BLOCKED": "Engineering validation is BLOCKED. Quotation generation disabled.",
        "ENGINEERING_VALIDATION": "No engineering validation verdict supplied.",
        "COMMERCIAL_SNAPSHOT": "No ISSUED commercial snapshot. Required commercial values do not exist.",
        "SUBSIDY_RESOLVED": "Subsidy treatment unresolved. No Subsidy Engine exists, so the operator must record "
        "subsidyTreatment = NOT_QUOTED to state explicitly that this quotation carries no subsidy.",
        "QUOTATION_METADATA": "Quotation metadata incomplete — quotationNumber, quotationVersion and quotationDate are required.",
    }
)

_BOM_SNAPSHOT_LOCKED = "LOCKED"
_COMMERCIAL_SNAPSHOT_ISSUED = "ISSUED"


@dataclass(frozen=True)
class CheckOutcome:
    check: GateCheck
    status: GateStatus
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check.value, "status": self.status.value, "reason": self.reason}


class GenerationBlocked(ValueError):
    """``GENERATION_BLOCKED``: issuance refused; ``failed_checks``/``reasons`` name every failed check."""

    code = "GENERATION_BLOCKED"

    def __init__(self, report: GateReport) -> None:
        self.report = report
        self.failed_checks = report.blocked_checks
        self.reasons = report.reasons
        self.message = f"Quotation generation disabled. Failed checks: {', '.join(check.value for check in report.blocked_checks)}."
        super().__init__(self.message)


@dataclass(frozen=True)
class GateReport:
    """``{status, checks[{check, status, reason}], blockedChecks, reasons}``."""

    status: GateStatus
    checks: tuple[CheckOutcome, ...]

    @property
    def passed(self) -> bool:
        return self.status == GateStatus.PASS

    @property
    def blocked_checks(self) -> tuple[GateCheck, ...]:
        return tuple(outcome.check for outcome in self.checks if outcome.status == GateStatus.BLOCKED)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{outcome.check.value}: {outcome.reason}" for outcome in self.checks if outcome.status == GateStatus.BLOCKED)

    def raise_if_blocked(self) -> None:
        """The issue path: refuse (and write nothing) unless every check passed."""
        if not self.passed:
            raise GenerationBlocked(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": [outcome.as_dict() for outcome in self.checks],
            "blockedChecks": [check.value for check in self.blocked_checks],
            "reasons": list(self.reasons),
        }


def evaluate_generation_gate(
    *,
    customer: Any = None,
    system: Any = None,
    bom_snapshot: Any = None,
    engineering: Any = None,
    commercial_snapshot: Any = None,
    subsidy_treatment: Any = None,
    quotation: Any = None,
) -> GateReport:
    """``evaluateGenerationGate``: all eight checks, each with its blocking reason (JavaScript truthiness)."""
    checks: list[CheckOutcome] = []

    def add(check: GateCheck, ok: bool, reason: str) -> None:
        checks.append(CheckOutcome(check, GateStatus.PASS if ok else GateStatus.BLOCKED, None if ok else reason))

    add(GateCheck.CUSTOMER_DATA, js_truthy(customer) and js_truthy(prop(customer, "customerId")) and js_truthy(prop(customer, "customerName")), GATE_REASONS["CUSTOMER_DATA"])
    add(
        GateCheck.SYSTEM_CONFIGURATION,
        js_truthy(system) and js_truthy(prop(system, "systemType")) and not is_nullish(prop(system, "systemSizeKw")) and js_truthy(prop(system, "phase")),
        GATE_REASONS["SYSTEM_CONFIGURATION"],
    )
    lines = prop(bom_snapshot, "lines")
    add(GateCheck.BOM_EXISTS, js_truthy(bom_snapshot) and isinstance(lines, (list, tuple)) and len(lines) > 0, GATE_REASONS["BOM_EXISTS"])
    add(GateCheck.BOM_LOCKED, js_truthy(bom_snapshot) and prop(bom_snapshot, "status") == _BOM_SNAPSHOT_LOCKED, GATE_REASONS["BOM_LOCKED"])
    verdict = prop(engineering, "status")
    engineering_blocked = js_truthy(engineering) and verdict == "BLOCKED"
    add(
        GateCheck.ENGINEERING_VALIDATION,
        js_truthy(engineering) and js_truthy(verdict) and verdict != "BLOCKED",
        GATE_REASONS["ENGINEERING_VALIDATION_BLOCKED"] if engineering_blocked else GATE_REASONS["ENGINEERING_VALIDATION"],
    )
    add(GateCheck.COMMERCIAL_SNAPSHOT, js_truthy(commercial_snapshot) and prop(commercial_snapshot, "status") == _COMMERCIAL_SNAPSHOT_ISSUED, GATE_REASONS["COMMERCIAL_SNAPSHOT"])
    add(GateCheck.SUBSIDY_RESOLVED, subsidy_treatment in (SubsidyTreatment.NOT_QUOTED.value, SubsidyTreatment.ENGINE_RESULT.value), GATE_REASONS["SUBSIDY_RESOLVED"])
    add(
        GateCheck.QUOTATION_METADATA,
        js_truthy(quotation) and js_truthy(prop(quotation, "quotationNumber")) and not is_nullish(prop(quotation, "quotationVersion")) and js_truthy(prop(quotation, "quotationDate")),
        GATE_REASONS["QUOTATION_METADATA"],
    )
    blocked = any(outcome.status == GateStatus.BLOCKED for outcome in checks)
    return GateReport(GateStatus.BLOCKED if blocked else GateStatus.PASS, tuple(checks))


def derive_subsidy_treatment(record: Any, subsidy_result: Any) -> Any:
    """``deriveSubsidyTreatment``: an explicit ``NOT_QUOTED`` on the record wins; an engine result (eligible or not)
    gives ``ENGINE_RESULT``; otherwise the declared value, defaulting to ``NOT_QUOTED``."""
    declared = prop(record, "subsidyTreatment")
    if declared == SubsidyTreatment.NOT_QUOTED:
        return SubsidyTreatment.NOT_QUOTED.value
    if js_truthy(subsidy_result):
        return SubsidyTreatment.ENGINE_RESULT.value
    return coalesce(declared, SubsidyTreatment.NOT_QUOTED.value)


def resolve_inputs(record: Mapping, version: Any, *, bom_snapshot: Any = None, commercial_snapshot: Any = None) -> Mapping:
    """``resolveInputs``: the gate/payload inputs of a quotation record at ``version`` (snapshots resolved by the caller
    from the record's ``bomSnapshotId``/``commercialSnapshotId``). Keys are the JavaScript ones."""
    quotation = {
        "quotationNumber": coalesce(prop(record, "quotationNumber"), None),
        "quotationVersion": version,
        "quotationDate": coalesce(prop(record, "quotationDate"), None),
        "validUntil": coalesce(prop(record, "validUntil"), None),
        "proposalBy": coalesce(prop(record, "proposalBy"), None),
        "salespersonId": coalesce(prop(record, "salespersonId"), None),
        "quotationLanguage": coalesce(prop(record, "quotationLanguage"), None),
        "status": prop(record, "status"),
        "quotationSource": coalesce(prop(record, "quotationSource"), "DIRECT"),
        "district": coalesce(prop(record, "district"), None),
        "affiliateId": coalesce(prop(record, "affiliateId"), None),
    }
    return deep_freeze(
        {
            "quotation": quotation,
            "customer": coalesce(prop(record, "customer"), None),
            "site": coalesce(prop(record, "site"), None),
            "system": coalesce(prop(record, "system"), None),
            "bomSnapshot": bom_snapshot if js_truthy(prop(record, "bomSnapshotId")) else None,
            "commercialSnapshot": commercial_snapshot if js_truthy(prop(record, "commercialSnapshotId")) else None,
            "engineering": coalesce(prop(record, "engineering"), None),
            "upgradeIdentity": coalesce(prop(record, "upgradeIdentity"), None),
            "subsidyTreatment": coalesce(prop(record, "subsidyTreatment"), None),
            "variant": coalesce(prop(record, "variant"), "FULL"),
        }
    )
