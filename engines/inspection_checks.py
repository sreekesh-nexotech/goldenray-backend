"""Equipment-location checklists of a site inspection and their status (PLAN §2.7, Plan 2 §3.2).

The three checklists are the Site Inspection V2 ``lib/equipment-assessment.ts`` definitions, word for word:
``ON_GRID_INVERTER`` (14 checks), ``HYBRID_INVERTER`` (16: the on-grid list without ``cable_route``, then a hybrid
``cable_route``, ``battery_distance``, ``battery_cable_length``) and ``HYBRID_BATTERY`` (19). Every check is worded
positively: PASS means the statement holds.

Definitions are versioned: ``site_inspections_equipment_assessment.checks_version`` pins the version an assessment
was answered against, and :func:`checklist` serves that version, so a later change of wording or of the check list
never re-scores old answers.

Status is computed over the **full definition** (the legacy scorer looked only at the keys submitted, so one PASS tap
made the whole assessment PASS): an unanswered check is ``NOT_CHECKED``; nothing answered → NOT_STARTED; any FAIL →
FAIL; any NEEDS_REVIEW → REQUIRES_REVIEW; any check still NOT_CHECKED → IN_PROGRESS; every check PASS or N/A → PASS.
A FAIL or REQUIRES_REVIEW assessment is resolved by an engineering review decision of RESOLVED or WAIVED. Because FAIL
and REQUIRES_REVIEW win over NOT_CHECKED, the status alone does not say the checklist was answered in full:
:func:`unanswered_checks` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

CHECKS_VERSION = "v1"


class EquipmentType(StrEnum):
    ON_GRID_INVERTER = "ON_GRID_INVERTER"
    HYBRID_INVERTER = "HYBRID_INVERTER"
    HYBRID_BATTERY = "HYBRID_BATTERY"


class SystemType(StrEnum):
    ON_GRID = "ON_GRID"
    HYBRID = "HYBRID"
    UNDECIDED = "UNDECIDED"


class CheckResult(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AssessmentStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    PASS = "PASS"
    FAIL = "FAIL"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    WAIVED = "WAIVED"


_RESULT_VALUES = frozenset(str(result) for result in CheckResult)

RESULT_LABEL = {
    CheckResult.NOT_CHECKED: "Not Checked",
    CheckResult.PASS: "Pass",
    CheckResult.FAIL: "Fail",
    CheckResult.NEEDS_REVIEW: "Needs Review",
    CheckResult.NOT_APPLICABLE: "N/A",
}

#: Statuses that need an engineering decision before installation (legacy ``isCriticalAssessmentStatus``).
CRITICAL_STATUSES = frozenset({AssessmentStatus.FAIL, AssessmentStatus.REQUIRES_REVIEW})
#: Review decisions that settle a critical assessment; WAIVED is an accepted risk and counts as resolved.
RESOLVING_REVIEW_STATUSES = frozenset({ReviewStatus.RESOLVED, ReviewStatus.WAIVED})


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    description: str
    requires_evidence_on_failure: bool = True


@dataclass(frozen=True)
class Checklist:
    equipment_type: EquipmentType
    title: str
    description: str
    checks: tuple[Check, ...]
    version: str = CHECKS_VERSION

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(check.id for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "equipment_type": str(self.equipment_type),
            "title": self.title,
            "description": self.description,
            "checks_version": self.version,
            "checks": [{"id": check.id, "label": check.label, "description": check.description, "requires_evidence_on_failure": check.requires_evidence_on_failure} for check in self.checks],
        }


_COMMON_INVERTER_V1 = (
    Check("direct_sunlight", "Direct Sunlight", "Inverter is not exposed to direct sunlight for long periods."),
    Check("rain_exposure", "Rain Exposure", "Rainwater cannot directly fall on the inverter."),
    Check("water_leakage", "Water Leakage", "No leakage from roof, wall, pipe, AC drain, etc. above or near inverter."),
    Check("ventilation", "Ventilation", "Open and ventilated area is available for heat dissipation."),
    Check("clearance", "Clearance", "Sufficient space is available around the inverter as required by the manufacturer."),
    Check("heat_source", "Heat Source", "Location is away from exhaust, boiler, kitchen, generator, AC outdoor unit, etc."),
    Check("dust_chemical_exposure", "Dust / Chemical Exposure", "Location is not exposed to excessive dust, cement, corrosive chemicals, fertilizer, salt, etc."),
    Check("physical_protection", "Physical Protection", "Location is protected from vehicle movement, impact, children, machinery, etc."),
    Check("maintenance_access", "Maintenance Access", "Technician can easily access the inverter for service and inspection."),
    Check("mounting_surface", "Mounting Surface", "Wall or structure is strong enough to support the inverter."),
    Check("cable_route", "Cable Route", "Safe and practical route is available for required cables."),
    Check("cable_entry", "Cable Entry", "Suitable entry point is available without unnecessary cable extension."),
    Check("height", "Installation Height", "Installation height is practical for operation and maintenance."),
    Check("flooding_water_risk", "Flooding / Water Risk", "Location is not vulnerable to water accumulation or flooding."),
)

_HYBRID_INVERTER_V1 = tuple(check for check in _COMMON_INVERTER_V1 if check.id != "cable_route") + (
    Check("cable_route", "Cable Route", "Safe route is available for PV, AC, battery and communication cables."),
    Check("battery_distance", "Battery Distance", "Suitable distance is available between the inverter and battery."),
    Check("battery_cable_length", "Battery Cable Length", "Battery cable length can be kept within manufacturer limits."),
)

_HYBRID_BATTERY_V1 = (
    Check("safe_location", "Safe Location", "Battery can be installed in a secure and controlled area."),
    Check("ventilation", "Ventilation", "Adequate ventilation and air circulation is available as required by the battery manufacturer."),
    Check("direct_sunlight", "Direct Sunlight", "Battery is protected from prolonged direct sunlight."),
    Check("rain_exposure", "Rain Exposure", "No direct rain exposure unless specifically rated for it."),
    Check("water_leakage", "Water Leakage", "No leakage from roof, pipes, walls or AC drains."),
    Check("dry_location", "Dry Location", "Area remains dry during normal weather conditions."),
    Check("temperature", "Ambient Temperature", "Ambient temperature is suitable for battery operation."),
    Check("heat_source", "Heat Source", "Battery is away from generators, boilers, kitchen equipment, etc."),
    Check("combustible_materials", "Combustible Materials", "No easily combustible materials are stored around the battery."),
    Check("chemical_exposure", "Chemical Exposure", "Battery is away from chemicals, corrosive materials and fuel storage."),
    Check("flooding", "Flooding", "Location is not low-lying or flood-prone."),
    Check("clearance", "Clearance", "Required front, side and top clearance is available."),
    Check("physical_damage", "Physical Damage", "Battery is protected from vehicles, machinery, impact and unauthorized access."),
    Check("maintenance_access", "Maintenance Access", "Technician can safely access the battery."),
    Check("battery_support", "Battery Support", "Floor, wall or stand can safely support battery weight."),
    Check("floor_condition", "Floor Condition", "Floor is level, stable and suitable."),
    Check("cable_routing", "Cable Routing", "Battery cables can be routed safely and protected."),
    Check("cable_length", "Battery Cable Length", "Battery-to-inverter distance is within the manufacturer's permitted range."),
    Check("emergency_access", "Emergency Access", "Battery can be safely accessed in case of emergency or service."),
)

DEFINITIONS: dict[str, dict[EquipmentType, Checklist]] = {
    "v1": {
        EquipmentType.ON_GRID_INVERTER: Checklist(EquipmentType.ON_GRID_INVERTER, "On-Grid Inverter", "Assess the proposed on-grid inverter installation location.", _COMMON_INVERTER_V1, "v1"),
        EquipmentType.HYBRID_INVERTER: Checklist(EquipmentType.HYBRID_INVERTER, "Hybrid Inverter", "Assess the proposed hybrid inverter installation location.", _HYBRID_INVERTER_V1, "v1"),
        EquipmentType.HYBRID_BATTERY: Checklist(EquipmentType.HYBRID_BATTERY, "Hybrid Battery", "Assess the proposed battery installation location.", _HYBRID_BATTERY_V1, "v1"),
    },
}


def _equipment_type(value: Any) -> EquipmentType:
    try:
        return EquipmentType(value)
    except ValueError as exc:
        raise ValueError(f"Unknown equipment type {value!r}.") from exc


def checklist(equipment_type: str, checks_version: str = CHECKS_VERSION) -> Checklist:
    """The check definition an assessment was answered against."""
    kind = _equipment_type(equipment_type)
    try:
        return DEFINITIONS[checks_version][kind]
    except KeyError as exc:
        raise ValueError(f"Unknown checks version {checks_version!r}.") from exc


def required_equipment_types(system_type: str) -> tuple[EquipmentType, ...]:
    """The assessments an installation of this system type needs; UNDECIDED needs a decision first, not none.

    Read from the inspection's ``system_type`` column only (the legacy equipment route read the snapshot and treated
    UNDECIDED as ON_GRID while readiness read the column).
    """
    try:
        kind = SystemType(system_type)
    except ValueError as exc:
        raise ValueError(f"Unknown system type {system_type!r}.") from exc
    if kind == SystemType.HYBRID:
        return (EquipmentType.HYBRID_INVERTER, EquipmentType.HYBRID_BATTERY)
    if kind == SystemType.ON_GRID:
        return (EquipmentType.ON_GRID_INVERTER,)
    return ()


def validate_results(equipment_type: str, results: Any, checks_version: str = CHECKS_VERSION) -> dict[str, str]:
    """Errors keyed by check id (``results`` for the document itself); empty when the answers are valid."""
    definition = checklist(equipment_type, checks_version)
    if not isinstance(results, Mapping):
        return {"results": "Must be an object of check id → result."}
    errors: dict[str, str] = {}
    known = set(definition.ids)
    for check_id, value in results.items():
        if check_id not in known:
            errors[str(check_id)] = f"Unknown check for {definition.equipment_type} ({definition.version})."
        elif not isinstance(value, str) or value not in _RESULT_VALUES:
            errors[str(check_id)] = f"Unknown result {value!r}; allowed: {', '.join(CheckResult)}."
    return errors


def normalise_results(equipment_type: str, results: Mapping[str, Any] | None, checks_version: str = CHECKS_VERSION) -> dict[str, CheckResult]:
    """Every check of the definition, in definition order; an unanswered check is NOT_CHECKED. Invalid input raises."""
    results = results or {}
    errors = validate_results(equipment_type, results, checks_version)
    if errors:
        raise ValueError(f"Invalid equipment results: {errors}")
    return {check_id: CheckResult(results.get(check_id, CheckResult.NOT_CHECKED)) for check_id in checklist(equipment_type, checks_version).ids}


def compute_status(equipment_type: str, results: Mapping[str, Any] | None, checks_version: str = CHECKS_VERSION) -> AssessmentStatus:
    """The assessment status over the full check definition (see the module docstring)."""
    values = list(normalise_results(equipment_type, results, checks_version).values())
    if all(value == CheckResult.NOT_CHECKED for value in values):
        return AssessmentStatus.NOT_STARTED
    if CheckResult.FAIL in values:
        return AssessmentStatus.FAIL
    if CheckResult.NEEDS_REVIEW in values:
        return AssessmentStatus.REQUIRES_REVIEW
    if CheckResult.NOT_CHECKED in values:
        return AssessmentStatus.IN_PROGRESS
    return AssessmentStatus.PASS


def is_critical(status: str) -> bool:
    return status in CRITICAL_STATUSES


def unanswered_checks(equipment_type: str, results: Mapping[str, Any] | None, checks_version: str = CHECKS_VERSION) -> tuple[str, ...]:
    """The checks of the definition still NOT_CHECKED (missing or explicit), in definition order.

    Completeness is read from the answers, never from the status: FAIL and REQUIRES_REVIEW win over NOT_CHECKED, so
    one FAIL tap scores FAIL with every other check unanswered.
    """
    return tuple(check_id for check_id, value in normalise_results(equipment_type, results, checks_version).items() if value == CheckResult.NOT_CHECKED)


def is_complete(status: str) -> bool:
    """The status is past NOT_STARTED/IN_PROGRESS. It does **not** say every check was answered — a FAIL or
    REQUIRES_REVIEW status can be partial; use :func:`unanswered_checks` for that."""
    return status not in (AssessmentStatus.NOT_STARTED, AssessmentStatus.IN_PROGRESS)


def is_resolved(status: str, review_status: str | None) -> bool:
    """Ready for installation: PASS, or a critical status whose review was RESOLVED or WAIVED (of a checklist with no
    check left unanswered — a review settles the answers given, it never answers the rest)."""
    if status == AssessmentStatus.PASS:
        return True
    return is_critical(status) and review_status in RESOLVING_REVIEW_STATUSES


def evidence_required(status: str) -> bool:
    """A failed assessment needs an evidence photo (Plan 2 D2-4)."""
    return status == AssessmentStatus.FAIL


def result_counts(equipment_type: str, results: Mapping[str, Any] | None, checks_version: str = CHECKS_VERSION) -> dict[str, int]:
    counts = dict.fromkeys((str(result) for result in CheckResult), 0)
    for value in normalise_results(equipment_type, results, checks_version).values():
        counts[str(value)] += 1
    return counts


def legacy_status(results: Mapping[str, Any]) -> str:
    """The legacy ``calculateAssessmentStatus`` verbatim (keys present only, ``CONDITIONAL`` for unknown values).

    Kept to report, at import, which stored legacy statuses the full-definition rule changes.
    """
    values = list(results.values())
    if not values or all(value == "NOT_CHECKED" for value in values):
        return "NOT_STARTED"
    if "FAIL" in values:
        return "FAIL"
    if "NEEDS_REVIEW" in values:
        return "REQUIRES_REVIEW"
    if "NOT_CHECKED" in values:
        return "IN_PROGRESS"
    if "NOT_APPLICABLE" in values and all(value == "PASS" for value in values if value != "NOT_APPLICABLE"):
        return "PASS"
    if all(value == "PASS" for value in values):
        return "PASS"
    return "CONDITIONAL"
