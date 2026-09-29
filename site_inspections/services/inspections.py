"""Inspection records: PRE_SALE creation, stage updates (allow-lists), assignment, system type, archive.

* ``create_inspection`` — Sales create PRE_SALE visits only (agreement inspections come from ``agreements.issued``);
  the number ``SV-YYYYMMDD-NNNN`` is reserved in the same transaction (office-local day, no preview endpoint, no
  overflow at 9,999); ``visit_date`` defaults to the local date; snapshot #1 is written.
* ``update_stage`` — the engineer's stage save: only that stage's columns (``services.stages``), inside the engineer
  write window (``common.ENGINEER_WRITABLE``); starts the work (→ IN_PROGRESS); auto-triggers engineering reviews.
* ``set_system_type`` — Project Head decision; after COMPLETED it sends the inspection back (REVISION_REQUIRED) and
  resets the equipment checklists that no longer apply.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.services import changes, snapshot
from core.errors import Conflict, DomainError, PermissionDenied
from core.sequences import next_number
from core.services import stamp_create
from engines.inspection_checks import required_equipment_types
from site_inspections.models import EquipmentAssessment, Inspection, LocationApproval
from site_inspections.models.choices import ApprovalStatus, Complexity, Origin, SnapshotSource, Status, SystemType
from site_inspections.services import common, reviews, snapshots, stages

CREATE_FIELDS = ("address", "pincode", "location", "district")
AFTER_COMPLETION = frozenset({Status.COMPLETED, Status.CUSTOMER_APPROVAL_PENDING, Status.APPROVED, Status.INSTALLATION_READY, Status.REJECTED})


def _check_engineer(engineer) -> None:
    if engineer is not None and not common.can_act(engineer, "edit"):
        raise DomainError("not_a_field_engineer", "This user cannot work on site inspections.", errors={"engineer_uid": ["The user has no site-inspection edit permission."]})


@transaction.atomic
def create_inspection(*, user, data: dict) -> Inspection:
    customer = data["customer"]
    engineer = data.get("engineer")
    if engineer is not None and not common.can_act(user, "assign"):
        raise PermissionDenied("assign_forbidden", "Assigning an engineer needs the site-inspection assign permission.", errors={"engineer_uid": ["Not allowed."]})
    _check_engineer(engineer)
    values = {name: data.get(name) or getattr(customer, name, "") or "" for name in CREATE_FIELDS}
    inspection = Inspection(
        number=next_number("SV"),
        visit_date=data.get("visit_date") or timezone.localdate(),
        customer=customer,
        engineer=engineer,
        origin=Origin.PRE_SALE,
        system_type=data.get("system_type") or SystemType.UNDECIDED,
        quotation_version_uid=data.get("quotation_version_uid"),
        **values,
    )
    stamp_create(inspection, user)
    inspection.save()
    snapshots.add(inspection, user=user, source=SnapshotSource.PRE_SALE, data=snapshots.build(inspection))
    common.audit("created", inspection, user, after={"number": inspection.number, "origin": inspection.origin, "customer": str(customer.uid), "engineer": str(engineer.uid) if engineer else None})
    common.changed(inspection)
    return inspection


def _coerce(stage_fields: tuple[str, ...], data: dict) -> dict:
    unknown = sorted(set(data) - set(stage_fields))
    if unknown:
        raise DomainError("field_not_allowed", "These fields cannot be written in this stage.", errors={name: ["Not writable in this stage."] for name in unknown})
    return data


def _derived(inspection: Inspection, values: dict) -> dict:
    """Values the stage save implies: the map link from the coordinates, the capture time of a new position."""
    lat = values.get("latitude", inspection.latitude)
    lng = values.get("longitude", inspection.longitude)
    if ("latitude" in values or "longitude" in values) and lat is not None and lng is not None:
        values["google_map_link"] = f"https://www.google.com/maps?q={Decimal(lat).normalize()},{Decimal(lng).normalize()}"
        values.setdefault("location_captured_at", timezone.now())
    return values


@transaction.atomic
def update_stage(instance: Inspection, *, user, stage_key: str, data: dict, expected_version=None) -> Inspection:
    stage = stages.stage(stage_key)
    if stage is None or not stage.fields:
        raise DomainError("unknown_stage", "This stage has no fields; use its own endpoint.", errors={"stage": [f"Use one of {', '.join(stages.EDITABLE_KEYS)}."]})
    inspection = common.lock(instance, expected_version)
    common.begin_work(inspection, user)
    values = _derived(inspection, dict(_coerce(stage.fields, data)))
    if values.get("complexity_status") == Complexity.ROUTINE and inspection.complexity_status == Complexity.ENGINEERING_REVIEW_REQUIRED and reviews.pending(inspection) is not None:
        raise Conflict("review_pending", "An engineering review is pending; only the reviewer can mark the site routine.", errors={"complexity_status": ["Review pending."]})
    values = {name: value for name, value in values.items() if getattr(inspection, name) != value}
    if not values:
        return inspection
    before = snapshot(inspection, list(values))
    inspection.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(inspection, list(values)))
    common.audit("stage_saved", inspection, user, before=changed_before, after={"stage": stage.key, **changed_after})
    reviews.field_triggers(before, values, user=user, inspection=inspection)
    common.changed(inspection)
    return inspection


@transaction.atomic
def start(instance: Inspection, *, user, expected_version=None) -> Inspection:
    """DRAFT / REVISION_REQUIRED → IN_PROGRESS (the engineer; also implied by any write)."""
    inspection = common.lock(instance, expected_version)
    if inspection.status == Status.IN_PROGRESS:
        return inspection
    common.begin_work(inspection, user)
    common.changed(inspection)
    return inspection


@transaction.atomic
def assign(instance: Inspection, *, user, engineer, expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    common.ensure_decision_open(inspection)
    _check_engineer(engineer)
    if inspection.engineer_id == getattr(engineer, "pk", None):
        return inspection
    previous = inspection.engineer
    inspection.versioned_update(user, engineer=engineer)
    common.audit("engineer_assigned", inspection, user, before={"engineer": str(previous.uid) if previous else None}, after={"engineer": str(engineer.uid) if engineer else None})
    common.changed(inspection)
    return inspection


def reset_equipment(inspection: Inspection, *, user) -> list[str]:
    """Soft-delete the checklists the current system type does not require; returns their types."""
    required = {str(kind) for kind in required_equipment_types(inspection.system_type)}
    removed = []
    for assessment in EquipmentAssessment.objects.filter(inspection=inspection).exclude(equipment_type__in=required):
        assessment.soft_delete(user)
        removed.append(assessment.equipment_type)
    return removed


def supersede_approvals(inspection: Inspection, *, user) -> list[int]:
    """PENDING and APPROVED approvals no longer stand once the inspection is sent back (history is kept)."""
    numbers = []
    for approval in LocationApproval.objects.filter(inspection=inspection, status__in=[ApprovalStatus.PENDING, ApprovalStatus.APPROVED]):
        approval.versioned_update(user, status=ApprovalStatus.SUPERSEDED)
        numbers.append(approval.number)
    return numbers


def send_back(inspection: Inspection, *, user, reason: str) -> None:
    """→ REVISION_REQUIRED: supersedes the standing approvals and clears a release (audited)."""
    superseded = supersede_approvals(inspection, user=user)
    extra = {"released_at": None, "released_by": None} if inspection.status == Status.INSTALLATION_READY else {}
    common.set_status(inspection, user, Status.REVISION_REQUIRED, note=reason, **extra)
    common.audit("revision_required", inspection, user, after={"reason": reason, "superseded_approvals": superseded})


@transaction.atomic
def set_system_type(instance: Inspection, *, user, system_type: str, reason: str = "", expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    common.ensure_decision_open(inspection)
    if inspection.status == Status.ON_HOLD:
        raise Conflict("invalid_status", "Resume the inspection before changing its system type.")
    if system_type == inspection.system_type:
        return inspection
    if inspection.status in AFTER_COMPLETION and not (reason or "").strip():
        raise DomainError("reason_required", "Say why the system type changes after the inspection was completed.", errors={"reason": ["Required."]})
    previous = inspection.system_type
    inspection.versioned_update(user, system_type=system_type)
    removed = reset_equipment(inspection, user=user)
    common.audit("system_type_changed", inspection, user, before={"system_type": previous}, after={"system_type": system_type, "reset_equipment": removed, "reason": reason})
    if inspection.status in AFTER_COMPLETION:
        send_back(inspection, user=user, reason=reason or f"System type changed from {previous} to {system_type}.")
    common.changed(inspection)
    return inspection


@transaction.atomic
def archive(instance: Inspection, *, user, expected_version=None) -> None:
    inspection = common.lock(instance, expected_version)
    if inspection.status in common.READ_ONLY:
        raise Conflict("inspection_read_only", "An approved or released inspection cannot be archived.", errors={"status": [inspection.status]})
    for approval in LocationApproval.objects.filter(inspection=inspection, status=ApprovalStatus.PENDING):
        approval.versioned_update(user, status=ApprovalStatus.SUPERSEDED)
    inspection.soft_delete(user)
    common.audit("archived", inspection, user, before={"status": inspection.status})
    common.changed(inspection)
