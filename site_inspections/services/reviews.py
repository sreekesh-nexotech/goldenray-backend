"""Engineering reviews (PLAN §2.7 ``site_inspections_engineering_review``, Plan 2 §3.2).

A review is created automatically when a condition that needs an engineer's judgement *becomes* true — complexity set
to ENGINEERING_REVIEW_REQUIRED (MANUAL), an equipment checklist turning FAIL/REQUIRES_REVIEW (EQUIPMENT), the
structure decision REQUIRES_ENGINEERING_REVIEW (STRUCTURE), roof condition REVIEW_REQUIRED (ROOF) or generation impact
REQUIRES_REVIEW (GENERATION) — or requested by hand. At most one review is PENDING (partial unique index): a second
trigger while one is pending is appended to its reason.

Deciding (``site_inspections.approve``): ROUTINE (also sets complexity ROUTINE), RESOLVED, or REQUIRES_CHANGES (the
engineer changes the site data and requests a new review). The engineer can no longer clear a review by re-selecting
"routine" (spec §J 4, 21).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.errors import Conflict, DomainError, NotFound
from core.services import check_version, stamp_create
from engines import inspection_readiness as ir
from site_inspections.models import EngineeringReview, Inspection
from site_inspections.models.choices import Complexity, GenerationImpact, ReviewDecision, ReviewTrigger, RoofCondition, StructureDecision
from site_inspections.services import common

DECISIONS = (ReviewDecision.ROUTINE, ReviewDecision.REQUIRES_CHANGES, ReviewDecision.RESOLVED)

#: Stage-field triggers: (trigger, column, value that needs a review, default reason).
FIELD_TRIGGERS = (
    (ReviewTrigger.MANUAL, "complexity_status", Complexity.ENGINEERING_REVIEW_REQUIRED, "Engineer marked the site as complex."),
    (ReviewTrigger.STRUCTURE, "site_structure_type", StructureDecision.REQUIRES_ENGINEERING_REVIEW, "Mounting structure requires engineering review."),
    (ReviewTrigger.ROOF, "roof_condition", RoofCondition.REVIEW_REQUIRED, "Roof condition requires review."),
    (ReviewTrigger.GENERATION, "generation_impact", GenerationImpact.REQUIRES_REVIEW, "Generation impact requires review."),
)


def pending(inspection: Inspection) -> EngineeringReview | None:
    return EngineeringReview.objects.filter(inspection=inspection, decision=ReviewDecision.PENDING).first()


def ensure_review(inspection: Inspection, *, trigger: str, reason: str, user=None) -> EngineeringReview:
    """The pending review (its reason extended), else a new PENDING review for ``trigger``."""
    review = pending(inspection)
    if review is not None:
        line = f"[{trigger}] {reason}".strip()
        if line not in review.reason:
            review.versioned_update(user, reason=f"{review.reason}\n{line}".strip())
        return review
    review = EngineeringReview(inspection=inspection, trigger=trigger, reason=f"[{trigger}] {reason}".strip(), requested_by=user if getattr(user, "pk", None) else None)
    stamp_create(review, user)
    review.save()
    common.audit("review_requested", inspection, user, after={"review": str(review.uid), "trigger": trigger, "reason": reason})
    return review


def field_triggers(before: dict, after: dict, *, user, inspection: Inspection) -> list[str]:
    """Create reviews for the stage conditions that became true with this update; returns the triggers fired."""
    fired = []
    for trigger, column, value, reason in FIELD_TRIGGERS:
        if column in after and after[column] == value and before.get(column) != value:
            text = inspection.complexity_reason if trigger == ReviewTrigger.MANUAL and inspection.complexity_reason else reason
            ensure_review(inspection, trigger=trigger, reason=text, user=user)
            fired.append(str(trigger))
    return fired


def review_pending(inspection: Inspection) -> bool:
    return ir.engineering_review_pending(common.build_state(inspection))


@transaction.atomic
def request_review(instance: Inspection, *, user, reason: str, expected_version=None) -> EngineeringReview:
    """A manual review request (the engineer after REQUIRES_CHANGES, or anyone with edit)."""
    inspection = common.lock(instance, expected_version)
    common.ensure_engineer_writable(inspection)
    if pending(inspection) is not None:
        raise Conflict("review_pending", "An engineering review is already pending for this inspection.")
    review = ensure_review(inspection, trigger=ReviewTrigger.MANUAL, reason=reason, user=user)
    common.changed(inspection)
    return review


def get_review(inspection: Inspection, uid) -> EngineeringReview:
    review = EngineeringReview.objects.filter(inspection=inspection, uid=uid).select_related("requested_by", "reviewer").first()
    if review is None:
        raise NotFound("not_found", "Engineering review not found.")
    return review


@transaction.atomic
def decide_review(review: EngineeringReview, *, user, decision: str, notes: str = "", expected_version=None) -> EngineeringReview:
    inspection = common.lock(review.inspection)
    common.ensure_decision_open(inspection)
    review = EngineeringReview.objects.select_for_update().get(pk=review.pk)
    check_version(review, expected_version)
    if review.decision != ReviewDecision.PENDING:
        raise Conflict("review_decided", "This engineering review was already decided.", errors={"decision": [review.decision]})
    if decision not in DECISIONS:
        raise DomainError("validation_error", "Choose ROUTINE, REQUIRES_CHANGES or RESOLVED.", errors={"decision": ["Invalid decision."]})
    if decision == ReviewDecision.REQUIRES_CHANGES and not (notes or "").strip():
        raise DomainError("notes_required", "Say what has to change.", errors={"notes": ["Required when changes are requested."]})
    review.versioned_update(user, decision=decision, notes=notes or "", reviewer=user, decided_at=timezone.now())
    if decision == ReviewDecision.ROUTINE and inspection.complexity_status != Complexity.ROUTINE:
        inspection.versioned_update(user, complexity_status=Complexity.ROUTINE)
    common.audit("review_decided", inspection, user, after={"review": str(review.uid), "trigger": review.trigger, "decision": decision, "notes": notes})
    common.changed(inspection)
    return review
