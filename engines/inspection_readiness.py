"""Installation readiness and field completion of a site inspection (PLAN §2.7/§2.9, Plan 2 §3.2).

Port of Site Inspection V2 ``getReadiness`` / ``getFieldCompletion`` (``lib/site-inspection.ts``) returning blocker
**codes** — the human text is a presentation map (:data:`BLOCKER_TEXT`) and nobody matches on substrings any more.
The 19 checks run in the PLAN §2.9 order (:data:`STEPS`); a step may yield one of several codes.

Fixes against the legacy engine (spec §C and §J):

* suitability is an enum: CONDITIONAL stays CONDITIONAL (legacy wrote 0 and read NOT_SUITABLE) and an unknown value
  is unconfirmed, not a pass — :func:`normalise_suitability` maps legacy values;
* neutral link and termination point are two fields compared as enums, each needing its own additional-work decision
  (legacy collapsed them and compared ``"not available"`` against the stored ``NOT_AVAILABLE``, so it never fired);
* equipment status is recomputed from the answers over the full check definition; a FAIL/REQUIRES_REVIEW assessment
  is one blocker, cleared by a RESOLVED or WAIVED review; an UNDECIDED system type blocks instead of waiving the checks;
* the customer approval stores a location snapshot and :func:`location_snapshot_matches` compares it with the current
  annotations (legacy never stored one, so an outdated approval could not be detected); a missing snapshot fails closed;
* only *current* annotations document a location; a rectangle still in the legacy container space must be re-drawn
  before release (Plan 2 D2-13);
* additional work is per item with an explicit ``required`` flag (legacy inferred it from nearly every inspection);
  customer-impacting work blocks release until approved (PLAN D-12);
* ``has_value(0)`` is a value; measurements must be positive;
* evaluation is over the state it is given — call it on ``state.with_changes(...)`` so a transition is judged on the row
  as it will be, not the row before the update;
* field completion (enforced at submit) no longer demands SUITABLE — only a recorded verdict — and includes the
  equipment assessments and a pending engineering review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Hashable, Iterable, Mapping

from engines.inspection_checks import CHECKS_VERSION, AssessmentStatus, EquipmentType, ReviewStatus, SystemType, compute_status, is_complete, is_resolved, required_equipment_types


class Suitability(StrEnum):
    SUITABLE = "SUITABLE"
    CONDITIONAL = "CONDITIONAL"
    NOT_SUITABLE = "NOT_SUITABLE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NEEDS_MODIFICATION = "NEEDS_MODIFICATION"


class ComplexityStatus(StrEnum):
    NOT_ASSESSED = "NOT_ASSESSED"
    ROUTINE = "ROUTINE"
    ENGINEERING_REVIEW_REQUIRED = "ENGINEERING_REVIEW_REQUIRED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class ReviewDecision(StrEnum):
    PENDING = "PENDING"
    ROUTINE = "ROUTINE"
    REQUIRES_CHANGES = "REQUIRES_CHANGES"
    RESOLVED = "RESOLVED"


class AdditionalWorkStatus(StrEnum):
    NONE = "NONE"
    IDENTIFIED = "IDENTIFIED"
    ENGINEERING_REVIEW = "ENGINEERING_REVIEW"
    COST_CALCULATED = "COST_CALCULATED"
    CUSTOMER_QUOTE_SENT = "CUSTOMER_QUOTE_SENT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AnnotationType(StrEnum):
    PANEL_AREA = "PANEL_AREA"
    EQUIPMENT_AREA = "EQUIPMENT_AREA"


class GeometrySpace(StrEnum):
    IMAGE = "IMAGE"
    LEGACY_CONTAINER = "LEGACY_CONTAINER"


#: The additional-work types that settle a missing/defective neutral link and termination point.
NEUTRAL_LINK_WORK = "NEW_NEUTRAL_LINK"
TERMINATION_WORK = "NEW_TERMINATION"
#: A customer-impacting item blocks release until it is in one of these (Plan 2 D2-3, PLAN D-12).
CUSTOMER_CLEARED_WORK = frozenset({AdditionalWorkStatus.NONE, AdditionalWorkStatus.APPROVED})


class Code(StrEnum):
    CUSTOMER_REQUIRED = "CUSTOMER_REQUIRED"
    ENGINEER_REQUIRED = "ENGINEER_REQUIRED"
    VISIT_DATE_REQUIRED = "VISIT_DATE_REQUIRED"
    PANEL_PHOTO_REQUIRED = "PANEL_PHOTO_REQUIRED"
    EQUIPMENT_PHOTO_REQUIRED = "EQUIPMENT_PHOTO_REQUIRED"
    PANEL_WIDTH_REQUIRED = "PANEL_WIDTH_REQUIRED"
    PANEL_HEIGHT_REQUIRED = "PANEL_HEIGHT_REQUIRED"
    EQUIPMENT_WIDTH_REQUIRED = "EQUIPMENT_WIDTH_REQUIRED"
    EQUIPMENT_HEIGHT_REQUIRED = "EQUIPMENT_HEIGHT_REQUIRED"
    LOCATIONS_NOT_DOCUMENTED = "LOCATIONS_NOT_DOCUMENTED"
    RESTRICTIONS_NEED_REMARKS = "RESTRICTIONS_NEED_REMARKS"
    SUITABILITY_UNCONFIRMED = "SUITABILITY_UNCONFIRMED"
    SITE_NOT_SUITABLE = "SITE_NOT_SUITABLE"
    SUITABILITY_CONDITIONAL = "SUITABILITY_CONDITIONAL"
    CUSTOMER_APPROVAL_REQUIRED = "CUSTOMER_APPROVAL_REQUIRED"
    CUSTOMER_APPROVAL_OUTDATED = "CUSTOMER_APPROVAL_OUTDATED"
    ENGINEERING_REVIEW_PENDING = "ENGINEERING_REVIEW_PENDING"
    ADDITIONAL_WORK_REJECTED = "ADDITIONAL_WORK_REJECTED"
    ADDITIONAL_WORK_UNAPPROVED = "ADDITIONAL_WORK_UNAPPROVED"
    EQUIPMENT_SYSTEM_TYPE_UNDECIDED = "EQUIPMENT_SYSTEM_TYPE_UNDECIDED"
    EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE = "EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE"
    EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION = "EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION"
    EQUIPMENT_HYBRID_INVERTER_INCOMPLETE = "EQUIPMENT_HYBRID_INVERTER_INCOMPLETE"
    EQUIPMENT_HYBRID_INVERTER_NEEDS_RESOLUTION = "EQUIPMENT_HYBRID_INVERTER_NEEDS_RESOLUTION"
    EQUIPMENT_HYBRID_BATTERY_INCOMPLETE = "EQUIPMENT_HYBRID_BATTERY_INCOMPLETE"
    EQUIPMENT_HYBRID_BATTERY_NEEDS_RESOLUTION = "EQUIPMENT_HYBRID_BATTERY_NEEDS_RESOLUTION"
    WHEELING_CONSUMER_NUMBER_REQUIRED = "WHEELING_CONSUMER_NUMBER_REQUIRED"
    WHEELING_PHONE_REQUIRED = "WHEELING_PHONE_REQUIRED"
    NEUTRAL_OR_TERMINATION_DECISION_REQUIRED = "NEUTRAL_OR_TERMINATION_DECISION_REQUIRED"
    LATEST_APPROVAL_NOT_APPROVED = "LATEST_APPROVAL_NOT_APPROVED"
    # field completion only (legacy getFieldCompletion)
    SITE_ADDRESS_REQUIRED = "SITE_ADDRESS_REQUIRED"
    COMPLEXITY_NOT_ASSESSED = "COMPLEXITY_NOT_ASSESSED"
    # warning (never blocks field completion; blocks readiness as LOCATIONS_NOT_DOCUMENTED)
    LEGACY_ANNOTATION_GEOMETRY = "LEGACY_ANNOTATION_GEOMETRY"


def incomplete_code(equipment_type: str) -> Code:
    return Code(f"EQUIPMENT_{EquipmentType(equipment_type)}_INCOMPLETE")


def needs_resolution_code(equipment_type: str) -> Code:
    return Code(f"EQUIPMENT_{EquipmentType(equipment_type)}_NEEDS_RESOLUTION")


#: The 19 readiness checks in PLAN §2.9 evaluation order, with the codes each may yield.
STEPS: tuple[tuple[Code, ...], ...] = (
    (Code.CUSTOMER_REQUIRED,),
    (Code.ENGINEER_REQUIRED,),
    (Code.VISIT_DATE_REQUIRED,),
    (Code.PANEL_PHOTO_REQUIRED,),
    (Code.EQUIPMENT_PHOTO_REQUIRED,),
    (Code.PANEL_WIDTH_REQUIRED,),
    (Code.PANEL_HEIGHT_REQUIRED,),
    (Code.EQUIPMENT_WIDTH_REQUIRED,),
    (Code.EQUIPMENT_HEIGHT_REQUIRED,),
    (Code.LOCATIONS_NOT_DOCUMENTED,),
    (Code.RESTRICTIONS_NEED_REMARKS,),
    (Code.SUITABILITY_UNCONFIRMED, Code.SITE_NOT_SUITABLE, Code.SUITABILITY_CONDITIONAL),
    (Code.CUSTOMER_APPROVAL_REQUIRED, Code.CUSTOMER_APPROVAL_OUTDATED),
    (Code.ENGINEERING_REVIEW_PENDING,),
    (Code.ADDITIONAL_WORK_REJECTED, Code.ADDITIONAL_WORK_UNAPPROVED),
    (Code.EQUIPMENT_SYSTEM_TYPE_UNDECIDED,) + tuple(code for kind in EquipmentType for code in (incomplete_code(kind), needs_resolution_code(kind))),
    (Code.WHEELING_CONSUMER_NUMBER_REQUIRED, Code.WHEELING_PHONE_REQUIRED),
    (Code.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,),
    (Code.LATEST_APPROVAL_NOT_APPROVED,),
)
STEP_OF: dict[Code, int] = {code: number for number, codes in enumerate(STEPS, start=1) for code in codes}

#: The codes :func:`field_completion` can return, in its order.
FIELD_COMPLETION_CODES: tuple[Code, ...] = (
    Code.CUSTOMER_REQUIRED,
    Code.ENGINEER_REQUIRED,
    Code.VISIT_DATE_REQUIRED,
    Code.SITE_ADDRESS_REQUIRED,
    Code.PANEL_PHOTO_REQUIRED,
    Code.EQUIPMENT_PHOTO_REQUIRED,
    Code.PANEL_WIDTH_REQUIRED,
    Code.PANEL_HEIGHT_REQUIRED,
    Code.EQUIPMENT_WIDTH_REQUIRED,
    Code.EQUIPMENT_HEIGHT_REQUIRED,
    Code.LOCATIONS_NOT_DOCUMENTED,
    Code.SUITABILITY_UNCONFIRMED,
    Code.COMPLEXITY_NOT_ASSESSED,
    Code.ENGINEERING_REVIEW_PENDING,
) + tuple(incomplete_code(kind) for kind in EquipmentType)

_EQUIPMENT_NAME = {kind: kind.value.replace("_", " ") for kind in EquipmentType}

#: Presentation text per code (the legacy wording where a legacy blocker existed).
BLOCKER_TEXT: dict[Code, str] = {
    Code.CUSTOMER_REQUIRED: "Customer is required",
    Code.ENGINEER_REQUIRED: "Assigned engineer is required",
    Code.VISIT_DATE_REQUIRED: "Site visit date is required",
    Code.PANEL_PHOTO_REQUIRED: "Proposed panel-area reference photo is required",
    Code.EQUIPMENT_PHOTO_REQUIRED: "Proposed inverter + ACDB + DCDB reference photo is required",
    Code.PANEL_WIDTH_REQUIRED: "Panel area measured width is required",
    Code.PANEL_HEIGHT_REQUIRED: "Panel area measured height is required",
    Code.EQUIPMENT_WIDTH_REQUIRED: "Equipment area measured width is required",
    Code.EQUIPMENT_HEIGHT_REQUIRED: "Equipment area measured height is required",
    Code.LOCATIONS_NOT_DOCUMENTED: "Both proposed installation locations must be documented",
    Code.RESTRICTIONS_NEED_REMARKS: "Location restrictions require customer remarks",
    Code.SUITABILITY_UNCONFIRMED: "Site suitability has not been confirmed",
    Code.SITE_NOT_SUITABLE: "Site is not suitable for solar installation",
    Code.SUITABILITY_CONDITIONAL: "Conditional site suitability requires resolution before installation",
    Code.CUSTOMER_APPROVAL_REQUIRED: "Customer-approved installation location is required",
    Code.CUSTOMER_APPROVAL_OUTDATED: "Customer approval is outdated because the proposed installation location changed",
    Code.ENGINEERING_REVIEW_PENDING: "Engineering review is required for this site",
    Code.ADDITIONAL_WORK_REJECTED: "Additional work/cost has been rejected",
    Code.ADDITIONAL_WORK_UNAPPROVED: "Customer-impacting additional work is awaiting the customer's approval",
    Code.EQUIPMENT_SYSTEM_TYPE_UNDECIDED: "System type must be decided before the equipment can be assessed",
    **{incomplete_code(kind): f"{_EQUIPMENT_NAME[kind]} assessment is incomplete" for kind in EquipmentType},
    **{needs_resolution_code(kind): f"{_EQUIPMENT_NAME[kind]} requires engineering resolution" for kind in EquipmentType},
    Code.WHEELING_CONSUMER_NUMBER_REQUIRED: "Consumer Number is required for wheeling",
    Code.WHEELING_PHONE_REQUIRED: "Registered Phone is required for wheeling",
    Code.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED: "Neutral Link / Termination Point requires an additional-work decision",
    Code.LATEST_APPROVAL_NOT_APPROVED: "Latest installation location approval is not approved",
    Code.SITE_ADDRESS_REQUIRED: "Site address is required",
    Code.COMPLEXITY_NOT_ASSESSED: "Site complexity has not been assessed",
    Code.LEGACY_ANNOTATION_GEOMETRY: "An installation-location rectangle was drawn in the legacy app and must be re-drawn before release",
}


# ---------------------------------------------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------------------------------------------


def has_value(value: Any) -> bool:
    """Whether a field holds an answer. ``0`` and ``False`` are answers (legacy ``hasValue(0)`` was false)."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, float):
        return not math.isnan(value)
    if isinstance(value, Decimal):
        return not value.is_nan()
    return True


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def is_positive(value: Any) -> bool:
    """A measurement: a finite number greater than zero."""
    number = _number(value)
    return number is not None and number > 0


_SUITABLE_ALIASES = {"1", "YES", "TRUE", "SUITABLE"}
_CONDITIONAL_ALIASES = {"CONDITIONAL", "CONDITIONALLY_SUITABLE", "CONDITIONAL SUITABLE", "CONDITIONALLY SUITABLE"}
_NOT_SUITABLE_ALIASES = {"0", "NO", "FALSE", "NOT_SUITABLE", "NOT SUITABLE"}


def _suitability_of(value: Any) -> Suitability | None:
    if value is None or value == "":
        return None
    if value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1):
        return Suitability.SUITABLE
    if value is False or (isinstance(value, int) and not isinstance(value, bool) and value == 0):
        return Suitability.NOT_SUITABLE
    text = str(value).strip().upper()
    if text in _SUITABLE_ALIASES:
        return Suitability.SUITABLE
    if text in _CONDITIONAL_ALIASES:
        return Suitability.CONDITIONAL
    if text in _NOT_SUITABLE_ALIASES:
        return Suitability.NOT_SUITABLE
    return None


def normalise_suitability(final_recommendation: Any = None, site_suitable_for_solar: Any = None) -> Suitability | None:
    """The v4 ``site_suitability`` for legacy values: the engineer's recommendation wins over the 1/0 flag it was
    squeezed into (a CONDITIONAL recommendation stored 0); anything unrecognised is ``None`` — unconfirmed."""
    return _suitability_of(final_recommendation) or _suitability_of(site_suitable_for_solar)


def normalise_availability(value: Any) -> Availability | None:
    """``AVAILABLE`` / ``NOT_AVAILABLE`` / ``NEEDS_MODIFICATION`` from any legacy spelling; blank is ``None``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return Availability(text)
    except ValueError as exc:
        raise ValueError(f"Unknown neutral-link/termination value {value!r}.") from exc


def _enum(kind: type[StrEnum], value: Any, what: str, *, optional: bool = False):
    if value is None and optional:
        return None
    try:
        return kind(value)
    except ValueError as exc:
        raise ValueError(f"Unknown {what} {value!r}; allowed: {', '.join(kind)}.") from exc


# ---------------------------------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Annotation:
    """``site_inspections_annotation``: a rectangle ``{x, y, w, h}`` in 0–1 image space on one photo."""

    annotation_type: str
    photo: Hashable
    geometry: Mapping[str, Any]
    geometry_space: str = GeometrySpace.IMAGE
    width_m: Any = None
    height_m: Any = None
    area_m2: Any = None
    is_current: bool = True
    number: int = 1

    def __post_init__(self):
        object.__setattr__(self, "annotation_type", _enum(AnnotationType, self.annotation_type, "annotation type"))
        object.__setattr__(self, "geometry_space", _enum(GeometrySpace, self.geometry_space, "geometry space"))


@dataclass(frozen=True)
class Approval:
    """``site_inspections_location_approval``: ``location_snapshot`` as stored when the approval was requested."""

    number: int
    status: str
    location_snapshot: Mapping[str, Any] | None = None

    def __post_init__(self):
        object.__setattr__(self, "status", _enum(ApprovalStatus, self.status, "approval status"))


@dataclass(frozen=True)
class EquipmentAssessment:
    """``site_inspections_equipment_assessment``; the status is recomputed from ``results``, never trusted."""

    equipment_type: str
    results: Mapping[str, Any] = field(default_factory=dict)
    checks_version: str = CHECKS_VERSION
    review_status: str = ReviewStatus.NOT_REQUIRED

    def __post_init__(self):
        object.__setattr__(self, "equipment_type", _enum(EquipmentType, self.equipment_type, "equipment type"))
        object.__setattr__(self, "review_status", _enum(ReviewStatus, self.review_status, "review status"))

    @property
    def status(self) -> AssessmentStatus:
        return compute_status(self.equipment_type, self.results, self.checks_version)


@dataclass(frozen=True)
class AdditionalWork:
    """``site_inspections_additional_work_item``."""

    work_type: str
    required: bool = True
    customer_impacting: bool = False
    status: str = AdditionalWorkStatus.IDENTIFIED

    def __post_init__(self):
        object.__setattr__(self, "work_type", str(self.work_type).strip().upper())
        object.__setattr__(self, "status", _enum(AdditionalWorkStatus, self.status, "additional-work status"))


@dataclass(frozen=True)
class EngineeringReview:
    """``site_inspections_engineering_review``; ``sequence`` orders reviews (creation order)."""

    decision: str
    sequence: int = 0

    def __post_init__(self):
        object.__setattr__(self, "decision", _enum(ReviewDecision, self.decision, "review decision"))


_TUPLE_FIELDS = ("annotations", "approvals", "equipment", "additional_work", "engineering_reviews")


@dataclass(frozen=True)
class InspectionState:
    """Everything readiness reads, as plain values (uids for references). Build it from the *current* row."""

    customer: Hashable | None = None
    engineer: Hashable | None = None
    visit_date: date | None = None
    address: str | None = None
    panel_photo: Hashable | None = None
    equipment_photo: Hashable | None = None
    panel_width_m: Any = None
    panel_height_m: Any = None
    equipment_width_m: Any = None
    equipment_height_m: Any = None
    has_location_restrictions: bool = False
    customer_location_remarks: str | None = None
    site_suitability: str | None = None
    complexity_status: str = ComplexityStatus.NOT_ASSESSED
    system_type: str = SystemType.UNDECIDED
    wheeling_required: bool = False
    consumer_number: Any = None
    registered_phone_e164: Any = None
    neutral_link: str | None = None
    termination_point: str | None = None
    annotations: tuple[Annotation, ...] = ()
    approvals: tuple[Approval, ...] = ()
    equipment: tuple[EquipmentAssessment, ...] = ()
    additional_work: tuple[AdditionalWork, ...] = ()
    engineering_reviews: tuple[EngineeringReview, ...] = ()

    def __post_init__(self):
        for name in _TUPLE_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))
        object.__setattr__(self, "site_suitability", _enum(Suitability, self.site_suitability, "site suitability", optional=True))
        object.__setattr__(self, "complexity_status", _enum(ComplexityStatus, self.complexity_status, "complexity status"))
        object.__setattr__(self, "system_type", _enum(SystemType, self.system_type, "system type"))
        object.__setattr__(self, "neutral_link", _enum(Availability, self.neutral_link, "neutral link", optional=True))
        object.__setattr__(self, "termination_point", _enum(Availability, self.termination_point, "termination point", optional=True))
        kinds = [assessment.equipment_type for assessment in self.equipment]
        if len(kinds) != len(set(kinds)):
            raise ValueError("One equipment assessment per equipment type.")
        numbers = [approval.number for approval in self.approvals]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Approval numbers must be unique.")
        current = [annotation.annotation_type for annotation in self.annotations if annotation.is_current]
        if len(current) != len(set(current)):
            raise ValueError("One current annotation per annotation type.")

    def with_changes(self, **changes: Any) -> InspectionState:
        """The state after an update — evaluate transitions on this, not on the row before the change."""
        return replace(self, **changes)


# ---------------------------------------------------------------------------------------------------------------
# location snapshot (customer approval)
# ---------------------------------------------------------------------------------------------------------------

_GEOMETRY_PLACES = 6
_METRE_PLACES = 3
_GEOMETRY_KEYS = (("x", ("x",)), ("y", ("y",)), ("w", ("w", "width")), ("h", ("h", "height")))


def _fixed(value: Any, places: int) -> str | None:
    number = _number(value)
    if number is None:
        return None
    return str(number.quantize(Decimal(1).scaleb(-places)))


def _canonical_entry(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    geometry = entry.get("geometry") if isinstance(entry.get("geometry"), Mapping) else {}
    return {
        "photo": None if entry.get("photo") is None else str(entry.get("photo")),
        "geometry": {key: _fixed(next((geometry[alias] for alias in aliases if alias in geometry), None), _GEOMETRY_PLACES) for key, aliases in _GEOMETRY_KEYS},
        "geometry_space": None if entry.get("geometry_space") is None else str(entry.get("geometry_space")),
        "width_m": _fixed(entry.get("width_m"), _METRE_PLACES),
        "height_m": _fixed(entry.get("height_m"), _METRE_PLACES),
        "area_m2": _fixed(entry.get("area_m2"), _METRE_PLACES),
    }


def current_annotations(annotations: Iterable[Annotation]) -> dict[AnnotationType, Annotation]:
    return {annotation.annotation_type: annotation for annotation in annotations if annotation.is_current}


def build_location_snapshot(annotations: Iterable[Annotation]) -> dict[str, Any]:
    """The JSON stored on a location approval: both current rectangles (photo, geometry, measurements)."""
    current = current_annotations(annotations)
    snapshot: dict[str, Any] = {}
    for kind in AnnotationType:
        annotation = current.get(kind)
        snapshot[kind.value] = (
            None
            if annotation is None
            else _canonical_entry(
                {
                    "photo": annotation.photo,
                    "geometry": annotation.geometry,
                    "geometry_space": str(annotation.geometry_space),
                    "width_m": annotation.width_m,
                    "height_m": annotation.height_m,
                    "area_m2": annotation.area_m2,
                }
            )
        )
    return snapshot


def location_snapshot_matches(stored: Mapping[str, Any] | None, annotations: Iterable[Annotation]) -> bool:
    """Whether the approved location is still the current one. Re-saving the same rectangle is not a change; a new
    photo, a moved rectangle or new measurements are. No stored snapshot cannot prove anything: ``False``."""
    if not isinstance(stored, Mapping):
        return False
    current = build_location_snapshot(annotations)
    return {kind.value: _canonical_entry(stored.get(kind.value)) for kind in AnnotationType} == current


# ---------------------------------------------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Blocker:
    code: Code
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return BLOCKER_TEXT[self.code]

    @property
    def step(self) -> int | None:
        return STEP_OF.get(self.code)

    def as_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "text": self.text, "step": self.step, "details": dict(self.details)}


@dataclass(frozen=True)
class Readiness:
    ready: bool
    blockers: tuple[Blocker, ...]
    checks: Mapping[str, bool] = field(default_factory=dict)
    warnings: tuple[Blocker, ...] = ()
    suitability: str | None = None
    approved_location_number: int | None = None

    @property
    def codes(self) -> tuple[Code, ...]:
        return tuple(blocker.code for blocker in self.blockers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": [blocker.as_dict() for blocker in self.blockers],
            "warnings": [warning.as_dict() for warning in self.warnings],
            "checks": dict(self.checks),
            "suitability": None if self.suitability is None else str(self.suitability),
            "approved_location_number": self.approved_location_number,
        }


def _locations(state: InspectionState) -> tuple[list[str], list[str]]:
    """(annotation types without a current rectangle, current rectangles still in the legacy container space)."""
    current = current_annotations(state.annotations)
    missing = [kind.value for kind in AnnotationType if kind not in current]
    legacy = [kind.value for kind in AnnotationType if kind in current and current[kind].geometry_space == GeometrySpace.LEGACY_CONTAINER]
    return missing, legacy


def _latest_approved(state: InspectionState) -> Approval | None:
    approved = [approval for approval in state.approvals if approval.status == ApprovalStatus.APPROVED]
    return max(approved, key=lambda approval: approval.number) if approved else None


def engineering_review_pending(state: InspectionState) -> bool:
    """A review awaits a decision, the latest decision asked for changes, or the site was marked complex and no review
    has found it routine or resolved it."""
    reviews = sorted(state.engineering_reviews, key=lambda review: review.sequence)
    if any(review.decision == ReviewDecision.PENDING for review in reviews):
        return True
    if reviews and reviews[-1].decision == ReviewDecision.REQUIRES_CHANGES:
        return True
    if state.complexity_status == ComplexityStatus.ENGINEERING_REVIEW_REQUIRED:
        return not any(review.decision in (ReviewDecision.ROUTINE, ReviewDecision.RESOLVED) for review in reviews)
    return False


def _suitability_blocker(state: InspectionState) -> Blocker | None:
    if state.site_suitability is None:
        return Blocker(Code.SUITABILITY_UNCONFIRMED)
    if state.site_suitability == Suitability.NOT_SUITABLE:
        return Blocker(Code.SITE_NOT_SUITABLE)
    if state.site_suitability == Suitability.CONDITIONAL:
        return Blocker(Code.SUITABILITY_CONDITIONAL)
    return None


def _equipment_blockers(state: InspectionState, *, completion_only: bool) -> list[Blocker]:
    blockers: list[Blocker] = []
    if state.system_type == SystemType.UNDECIDED:
        if not completion_only:
            blockers.append(Blocker(Code.EQUIPMENT_SYSTEM_TYPE_UNDECIDED))
        return blockers
    assessments = {assessment.equipment_type: assessment for assessment in state.equipment}
    for kind in required_equipment_types(state.system_type):
        assessment = assessments.get(kind)
        status = assessment.status if assessment is not None else AssessmentStatus.NOT_STARTED
        if not is_complete(status):
            blockers.append(Blocker(incomplete_code(kind), {"status": str(status)}))
        elif not completion_only and not is_resolved(status, assessment.review_status):
            blockers.append(Blocker(needs_resolution_code(kind), {"status": str(status), "review_status": str(assessment.review_status)}))
    return blockers


def _work_types(items: Iterable[AdditionalWork]) -> list[str]:
    return sorted({item.work_type for item in items})


def _electrical_decisions(state: InspectionState) -> list[str]:
    """The fields (neutral link, termination point) that are missing or need modification without a planned fix."""
    planned = {item.work_type for item in state.additional_work if item.required}
    open_fields = []
    for name, value, work in (("neutral_link", state.neutral_link, NEUTRAL_LINK_WORK), ("termination_point", state.termination_point, TERMINATION_WORK)):
        if value in (Availability.NOT_AVAILABLE, Availability.NEEDS_MODIFICATION) and work not in planned:
            open_fields.append(name)
    return open_fields


def _basics(state: InspectionState, *, with_address: bool) -> list[Blocker]:
    blockers = []
    if not has_value(state.customer):
        blockers.append(Blocker(Code.CUSTOMER_REQUIRED))
    if not has_value(state.engineer):
        blockers.append(Blocker(Code.ENGINEER_REQUIRED))
    if state.visit_date is None:
        blockers.append(Blocker(Code.VISIT_DATE_REQUIRED))
    if with_address and not has_value(state.address):
        blockers.append(Blocker(Code.SITE_ADDRESS_REQUIRED))
    if not has_value(state.panel_photo):
        blockers.append(Blocker(Code.PANEL_PHOTO_REQUIRED))
    if not has_value(state.equipment_photo):
        blockers.append(Blocker(Code.EQUIPMENT_PHOTO_REQUIRED))
    for code, value in (
        (Code.PANEL_WIDTH_REQUIRED, state.panel_width_m),
        (Code.PANEL_HEIGHT_REQUIRED, state.panel_height_m),
        (Code.EQUIPMENT_WIDTH_REQUIRED, state.equipment_width_m),
        (Code.EQUIPMENT_HEIGHT_REQUIRED, state.equipment_height_m),
    ):
        if not is_positive(value):
            blockers.append(Blocker(code))
    return blockers


def evaluate(state: InspectionState) -> Readiness:
    """Installation readiness: ready when no blocker applies (gates APPROVED → INSTALLATION_READY)."""
    blockers = _basics(state, with_address=False)
    missing, legacy = _locations(state)
    if missing or legacy:
        blockers.append(Blocker(Code.LOCATIONS_NOT_DOCUMENTED, {"missing": missing, "legacy_geometry": legacy}))
    if state.has_location_restrictions and not has_value(state.customer_location_remarks):
        blockers.append(Blocker(Code.RESTRICTIONS_NEED_REMARKS))
    suitability = _suitability_blocker(state)
    if suitability is not None:
        blockers.append(suitability)

    approved = _latest_approved(state)
    approval_current = approved is not None and location_snapshot_matches(approved.location_snapshot, state.annotations)
    if approved is None:
        blockers.append(Blocker(Code.CUSTOMER_APPROVAL_REQUIRED))
    elif not approval_current:
        blockers.append(Blocker(Code.CUSTOMER_APPROVAL_OUTDATED, {"approval": approved.number, "snapshot_stored": approved.location_snapshot is not None}))

    review_pending = engineering_review_pending(state)
    if review_pending:
        blockers.append(Blocker(Code.ENGINEERING_REVIEW_PENDING))

    rejected = [item for item in state.additional_work if item.required and item.status == AdditionalWorkStatus.REJECTED]
    unapproved = [item for item in state.additional_work if item.required and item.customer_impacting and item.status not in CUSTOMER_CLEARED_WORK and item.status != AdditionalWorkStatus.REJECTED]
    if rejected:
        blockers.append(Blocker(Code.ADDITIONAL_WORK_REJECTED, {"work_types": _work_types(rejected)}))
    if unapproved:
        blockers.append(Blocker(Code.ADDITIONAL_WORK_UNAPPROVED, {"work_types": _work_types(unapproved)}))

    equipment = _equipment_blockers(state, completion_only=False)
    blockers.extend(equipment)

    wheeling_complete = True
    if state.wheeling_required:
        if not has_value(state.consumer_number):
            blockers.append(Blocker(Code.WHEELING_CONSUMER_NUMBER_REQUIRED))
            wheeling_complete = False
        if not has_value(state.registered_phone_e164):
            blockers.append(Blocker(Code.WHEELING_PHONE_REQUIRED))
            wheeling_complete = False

    electrical = _electrical_decisions(state)
    if electrical:
        blockers.append(Blocker(Code.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED, {"fields": electrical}))

    latest = max(state.approvals, key=lambda approval: approval.number) if state.approvals else None
    if latest is not None and latest.status != ApprovalStatus.APPROVED:
        blockers.append(Blocker(Code.LATEST_APPROVAL_NOT_APPROVED, {"approval": latest.number, "status": str(latest.status)}))

    checks = {
        "inspection_complete": has_value(state.customer) and has_value(state.engineer),
        "installation_evidence": has_value(state.panel_photo) and has_value(state.equipment_photo),
        "panel_measurement": is_positive(state.panel_width_m) and is_positive(state.panel_height_m),
        "equipment_measurement": is_positive(state.equipment_width_m) and is_positive(state.equipment_height_m),
        "locations_documented": not missing and not legacy,
        "customer_location_approved": approval_current,
        "suitability_confirmed": state.site_suitability == Suitability.SUITABLE,
        "engineering_review_clear": not review_pending,
        "additional_work_resolved": not rejected and not unapproved,
        "equipment_resolved": not equipment,
        "wheeling_complete": wheeling_complete,
        "electrical_decisions_complete": not electrical,
    }
    return Readiness(
        ready=not blockers,
        blockers=tuple(blockers),
        checks=checks,
        suitability=state.site_suitability,
        approved_location_number=approved.number if approved is not None else None,
    )


def field_completion(state: InspectionState) -> Readiness:
    """The subset enforced when the engineer submits (IN_PROGRESS → COMPLETED).

    A recorded suitability verdict is required, not SUITABLE (a NOT_SUITABLE site is still a completed inspection); the
    complexity must be assessed, no engineering review may be pending, and every required equipment assessment must be
    answered in full. A legacy-space rectangle is a warning here and a blocker at release.
    """
    blockers = _basics(state, with_address=True)
    missing, legacy = _locations(state)
    if missing:
        blockers.append(Blocker(Code.LOCATIONS_NOT_DOCUMENTED, {"missing": missing, "legacy_geometry": []}))
    if state.site_suitability is None:
        blockers.append(Blocker(Code.SUITABILITY_UNCONFIRMED))
    if state.complexity_status == ComplexityStatus.NOT_ASSESSED:
        blockers.append(Blocker(Code.COMPLEXITY_NOT_ASSESSED))
    if engineering_review_pending(state):
        blockers.append(Blocker(Code.ENGINEERING_REVIEW_PENDING))
    blockers.extend(_equipment_blockers(state, completion_only=True))
    warnings = (Blocker(Code.LEGACY_ANNOTATION_GEOMETRY, {"annotation_types": legacy}),) if legacy else ()
    return Readiness(ready=not blockers, blockers=tuple(blockers), warnings=warnings, suitability=state.site_suitability)


def state_fields() -> tuple[str, ...]:
    """The names :class:`InspectionState` takes (for callers mapping a row onto it)."""
    return tuple(f.name for f in fields(InspectionState))
