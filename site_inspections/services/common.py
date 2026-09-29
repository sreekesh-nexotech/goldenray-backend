"""Shared helpers of the site-inspection services: querysets, locking, status guards, audit, readiness state.

Write windows (enforced here for every write, not only in the UI — Site Inspection V2 spec §B.9, §J 6):

* the engineer's data (stages, photos, annotations, equipment answers, observations, identified additional work) is
  writable in DRAFT, IN_PROGRESS and REVISION_REQUIRED only; the first write of the assigned engineer moves a DRAFT
  or REVISION_REQUIRED inspection to IN_PROGRESS. After submit the record waits for review/approval; a Project Head
  sends it back with ``revision/``;
* decisions (engineering reviews, equipment waivers, additional-work transitions) are taken up to APPROVED;
* APPROVED and INSTALLATION_READY are read-only for everything but release (and, for APPROVED, those decisions);
  INSTALLATION_READY is final.
"""

from __future__ import annotations

from django.db.models import Max

from accounts.services.authz import can
from audit.services import record
from core import scopes
from core.errors import Conflict, NotFound
from core.services import check_version
from engines import inspection_readiness as ir
from flarize.cache_utils import bump
from site_inspections.models import AdditionalWorkItem, Annotation, EngineeringReview, EquipmentAssessment, Inspection, LocationApproval, Photo
from site_inspections.models.choices import Status

MODULE = "site_inspections"
CACHE_NAMESPACE = "site_inspections"
OBJECT_TYPE = "site_inspections.inspection"

ENGINEER_WRITABLE = frozenset({Status.DRAFT, Status.IN_PROGRESS, Status.REVISION_REQUIRED})
DECISION_OPEN = frozenset(set(Status) - {Status.INSTALLATION_READY})
READ_ONLY = frozenset({Status.APPROVED, Status.INSTALLATION_READY})


def inspections_queryset():
    return Inspection.objects.select_related("customer", "engineer", "quoted_panel", "quoted_inverter", "quoted_battery", "panel_photo", "equipment_photo", "released_by", "pre_sale_source")


def visible(user, queryset=None):
    """Inspections ``user`` may see (record scope of the ``site_inspections`` module; fails closed)."""
    return scopes.apply(queryset if queryset is not None else inspections_queryset(), user, MODULE)


def get_visible(user, uid) -> Inspection:
    inspection = visible(user).filter(uid=uid).first()
    if inspection is None:
        raise NotFound("not_found", "Site inspection not found.")
    return inspection


def lock(instance: Inspection, expected_version=None) -> Inspection:
    """The inspection row locked for this transaction, with the client's ``expected_version`` checked."""
    inspection = Inspection.objects.select_for_update().get(pk=instance.pk)
    check_version(inspection, expected_version)
    return inspection


def audit(action: str, inspection: Inspection, user, *, before: dict | None = None, after: dict | None = None, note: str = "", actor_kind: str | None = None) -> None:
    """Every write of the context is recorded on the inspection, so ``…/activity/`` is its audit trail."""
    record(f"site_inspections.{action}", obj=inspection, actor=user, actor_kind=actor_kind, before=before, after=after, note=note)


def changed(inspection: Inspection) -> None:
    bump(CACHE_NAMESPACE)


def ensure_engineer_writable(inspection: Inspection) -> None:
    if inspection.status in ENGINEER_WRITABLE:
        return
    if inspection.status in READ_ONLY:
        raise Conflict("inspection_read_only", "This inspection is read-only after the customer's approval.", errors={"status": [inspection.status]})
    raise Conflict("invalid_status", f"A {inspection.status} inspection cannot be edited; a Project Head must send it back for revision first.", errors={"status": [inspection.status]})


def ensure_decision_open(inspection: Inspection) -> None:
    if inspection.status not in DECISION_OPEN:
        raise Conflict("inspection_read_only", "This inspection has been released for installation and is final.", errors={"status": [inspection.status]})


def set_status(inspection: Inspection, user, status: str, *, note: str = "", **extra) -> None:
    previous = inspection.status
    inspection.versioned_update(user, status=status, **extra)
    audit("status_changed", inspection, user, before={"status": previous}, after={"status": status, **({"note": note} if note else {})})


def begin_work(inspection: Inspection, user) -> None:
    """A write on a DRAFT / REVISION_REQUIRED inspection starts (or resumes) the field work: → IN_PROGRESS."""
    ensure_engineer_writable(inspection)
    if inspection.status == Status.IN_PROGRESS:
        return
    if inspection.engineer_id is None:
        raise Conflict("engineer_required", "Assign a field engineer before the inspection is started.", errors={"engineer_uid": ["Required."]})
    set_status(inspection, user, Status.IN_PROGRESS)


def can_act(user, action: str) -> bool:
    return can(user, MODULE, action)


def next_number(queryset, field: str = "number") -> int:
    return (queryset.aggregate(value=Max(field))["value"] or 0) + 1


# --------------------------------------------------------------------------------------------------------------------
# readiness state (engines.inspection_readiness) — always built from the rows as they are *now*
# --------------------------------------------------------------------------------------------------------------------


def annotation_state(annotation: Annotation) -> ir.Annotation:
    return ir.Annotation(
        annotation_type=annotation.annotation_type,
        photo=str(annotation.photo.uid),
        geometry=annotation.geometry,
        geometry_space=annotation.geometry_space,
        width_m=annotation.width_m,
        height_m=annotation.height_m,
        area_m2=annotation.area_m2,
        is_current=annotation.is_current,
        number=annotation.number,
    )


def current_annotations(inspection: Inspection) -> list[Annotation]:
    return list(Annotation.objects.filter(inspection=inspection, is_current=True).select_related("photo"))


def location_snapshot(inspection: Inspection) -> dict:
    return ir.build_location_snapshot(annotation_state(annotation) for annotation in current_annotations(inspection))


def build_state(inspection: Inspection) -> ir.InspectionState:
    annotations = tuple(annotation_state(a) for a in Annotation.objects.filter(inspection=inspection, is_current=True).select_related("photo"))
    approvals = tuple(ir.Approval(number=a.number, status=a.status, location_snapshot=a.location_snapshot) for a in LocationApproval.objects.filter(inspection=inspection))
    equipment = tuple(
        ir.EquipmentAssessment(equipment_type=e.equipment_type, results=e.results or {}, checks_version=e.checks_version, review_status=e.review_status)
        for e in EquipmentAssessment.objects.filter(inspection=inspection)
    )
    work = tuple(
        ir.AdditionalWork(work_type=w.work_type, required=w.required, customer_impacting=w.customer_impacting, status=w.status) for w in AdditionalWorkItem.objects.filter(inspection=inspection)
    )
    reviews = tuple(ir.EngineeringReview(decision=r.decision, sequence=r.pk) for r in EngineeringReview.objects.filter(inspection=inspection))
    photo_uid = {p.pk: str(p.uid) for p in Photo.objects.filter(pk__in=[pk for pk in (inspection.panel_photo_id, inspection.equipment_photo_id) if pk])}
    return ir.InspectionState(
        customer=str(inspection.customer_id) if inspection.customer_id else None,
        engineer=str(inspection.engineer_id) if inspection.engineer_id else None,
        visit_date=inspection.visit_date,
        address=inspection.address,
        panel_photo=photo_uid.get(inspection.panel_photo_id),
        equipment_photo=photo_uid.get(inspection.equipment_photo_id),
        panel_width_m=inspection.panel_width_m,
        panel_height_m=inspection.panel_height_m,
        equipment_width_m=inspection.equipment_width_m,
        equipment_height_m=inspection.equipment_height_m,
        has_location_restrictions=inspection.has_location_restrictions,
        customer_location_remarks=inspection.customer_location_remarks,
        site_suitability=inspection.site_suitability or None,
        complexity_status=inspection.complexity_status,
        system_type=inspection.system_type,
        wheeling_required=bool(inspection.wheeling_required),
        consumer_number=inspection.consumer_number,
        registered_phone_e164=inspection.registered_phone_e164,
        neutral_link=inspection.neutral_link or None,
        termination_point=inspection.termination_point or None,
        annotations=annotations,
        approvals=approvals,
        equipment=equipment,
        additional_work=work,
        engineering_reviews=reviews,
    )


def readiness(inspection: Inspection) -> dict:
    state = build_state(inspection)
    ready = ir.evaluate(state)
    completion = ir.field_completion(state)
    return {**ready.as_dict(), "field_completion": completion.as_dict()}
