"""Equipment-location checklists (PLAN §2.7 ``site_inspections_equipment_assessment``; ``engines.inspection_checks``).

* The checklists an inspection needs come from its ``system_type`` column only (ON_GRID → on-grid inverter;
  HYBRID → hybrid inverter + battery); UNDECIDED needs a decision first (409 ``system_type_undecided``).
* ``PUT …/equipment/<type>/`` is a **full replace** validated against the pinned check definition: every check of the
  definition is stored (unanswered = NOT_CHECKED), the status is recomputed over the full definition (never taken from
  the client), omitted text fields are cleared and ``evidence_photo_uid`` may be cleared with ``null``.
* A FAIL needs an evidence photo of the same inspection (Plan 2 D2-4). A critical status (FAIL / REQUIRES_REVIEW)
  puts the checklist in review (``review_status=PENDING``) and opens an EQUIPMENT engineering review when it becomes
  critical; changed answers re-open a review that had been RESOLVED/WAIVED.
* ``…/equipment/<type>/review/`` (``approve``) settles a critical checklist: RESOLVED or WAIVED with a note.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from engines import inspection_checks as checks
from site_inspections.models import EquipmentAssessment, Inspection
from site_inspections.models.choices import AssessmentReview, ReviewTrigger, SystemType
from site_inspections.services import common, photos, reviews

TEXT_FIELDS = ("issue", "corrective_action", "engineer_remarks")


def required_types(inspection: Inspection) -> tuple[str, ...]:
    return tuple(str(kind) for kind in checks.required_equipment_types(inspection.system_type))


def assessments(inspection: Inspection) -> list[EquipmentAssessment]:
    """Saved checklists of the required types, and a fresh unsaved one for each required type not answered yet."""
    saved = {a.equipment_type: a for a in EquipmentAssessment.objects.filter(inspection=inspection).select_related("evidence_photo", "resolved_by")}
    result = []
    for kind in required_types(inspection):
        assessment = saved.get(kind)
        if assessment is None:
            assessment = EquipmentAssessment(inspection=inspection, equipment_type=kind, checks_version=checks.CHECKS_VERSION, results=_as_json(checks.normalise_results(kind, {})))
        result.append(assessment)
    return result


def _as_json(results: dict) -> dict:
    return {key: str(value) for key, value in results.items()}


def _required(inspection: Inspection, equipment_type: str) -> None:
    if inspection.system_type == SystemType.UNDECIDED:
        raise Conflict("system_type_undecided", "Decide the system type before assessing the equipment location.")
    if equipment_type not in required_types(inspection):
        raise DomainError(
            "equipment_not_required", f"A {inspection.system_type} system does not use this checklist.", errors={"equipment_type": [f"Use one of {', '.join(required_types(inspection))}."]}
        )


@transaction.atomic
def put_assessment(instance: Inspection, *, user, equipment_type: str, data: dict, expected_version=None) -> EquipmentAssessment:
    inspection = common.lock(instance)
    common.begin_work(inspection, user)
    _required(inspection, equipment_type)
    assessment = EquipmentAssessment.objects.select_for_update().filter(inspection=inspection, equipment_type=equipment_type).first()
    if assessment is not None:
        check_version(assessment, expected_version)
    version = assessment.checks_version if assessment is not None else checks.CHECKS_VERSION
    raw = data.get("results") or {}
    errors = checks.validate_results(equipment_type, raw, version)
    if errors:
        raise DomainError("invalid_results", "The checklist answers are not valid.", errors={f"results.{key}": [message] for key, message in errors.items()})
    results = _as_json(checks.normalise_results(equipment_type, raw, version))
    status = str(checks.compute_status(equipment_type, results, version))
    evidence = photos.get_photo(inspection, data["evidence_photo_uid"], field="evidence_photo_uid") if data.get("evidence_photo_uid") else None
    if checks.evidence_required(status) and evidence is None:
        raise DomainError("evidence_required", "A failed check needs an evidence photo.", errors={"evidence_photo_uid": ["Required when a check fails."]})
    critical = checks.is_critical(status)
    values = {
        "results": results,
        "status": status,
        "evidence_photo": evidence,
        **{name: data.get(name) or "" for name in TEXT_FIELDS},
    }
    was_critical = assessment is not None and checks.is_critical(assessment.status)
    if critical and (assessment is None or assessment.review_status == AssessmentReview.NOT_REQUIRED or assessment.results != results):
        values.update(review_status=AssessmentReview.PENDING, resolved_by=None, resolved_at=None, resolution_note="")
    elif not critical:
        values.update(review_status=AssessmentReview.NOT_REQUIRED, resolved_by=None, resolved_at=None, resolution_note="")
    if critical and not (values["issue"] or "").strip():
        raise DomainError("issue_required", "Describe what is wrong or uncertain.", errors={"issue": ["Required when a check fails or needs review."]})
    if assessment is None:
        assessment = EquipmentAssessment(inspection=inspection, equipment_type=equipment_type, checks_version=version, **values)
        stamp_create(assessment, user)
        assessment.save()
    else:
        assessment.versioned_update(user, **values)
    common.audit("equipment_saved", inspection, user, after={"equipment_type": equipment_type, "status": status, "counts": checks.result_counts(equipment_type, results, version)})
    if critical and not was_critical:
        reviews.ensure_review(inspection, trigger=ReviewTrigger.EQUIPMENT, reason=f"{equipment_type} assessment is {status}.", user=user)
    common.changed(inspection)
    return assessment


@transaction.atomic
def review_assessment(instance: Inspection, *, user, equipment_type: str, decision: str, note: str, expected_version=None) -> EquipmentAssessment:
    inspection = common.lock(instance)
    common.ensure_decision_open(inspection)
    assessment = EquipmentAssessment.objects.select_for_update().filter(inspection=inspection, equipment_type=equipment_type).first()
    if assessment is None or not checks.is_critical(assessment.status):
        raise Conflict("not_reviewable", "Only a failed or review-required checklist can be resolved or waived.")
    check_version(assessment, expected_version)
    if decision not in (AssessmentReview.RESOLVED, AssessmentReview.WAIVED):
        raise DomainError("validation_error", "Choose RESOLVED or WAIVED.", errors={"decision": ["Invalid decision."]})
    if not (note or "").strip():
        raise DomainError("note_required", "A resolution note is required.", errors={"note": ["Required."]})
    assessment.versioned_update(user, review_status=decision, resolved_by=user, resolved_at=timezone.now(), resolution_note=note.strip())
    common.audit("equipment_reviewed", inspection, user, after={"equipment_type": equipment_type, "decision": decision, "note": note.strip()})
    common.changed(inspection)
    return assessment
