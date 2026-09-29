"""Observations and additional-work items (PLAN §2.7, D-12).

Additional work — one item per ``work_type``, the seven-state lifecycle **enforced** (V2 defined it and never
checked it, spec §B.6)::

    NONE ─▶ IDENTIFIED ─▶ ENGINEERING_REVIEW ─▶ COST_CALCULATED ─▶ CUSTOMER_QUOTE_SENT ─▶ APPROVED | REJECTED
     ▲          ▲                                  (needs the EXTRA_STRUCTURE agreement)            │
     └──────────┴────────────────────────────────────────────────────────────────────────────────────┘

The engineer (``edit``) identifies items (create, edit and delete while IDENTIFIED, inside the engineer write
window) and may re-identify a NONE item; every other move is a decision (``approve``). COST_CALCULATED requires the
``agreement_uid`` of the EXTRA_STRUCTURE agreement that prices the work (agreements context, stored by uid); an
installed validator (:func:`register_agreement_validator`) can check it against the agreements context. Customer-
impacting items block release until APPROVED or NONE (readiness ``ADDITIONAL_WORK_UNAPPROVED``, D-12).
"""

from __future__ import annotations

from collections.abc import Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import changes, snapshot
from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from core.services import check_version, stamp_create
from site_inspections.models import AdditionalWorkItem, Inspection, Observation
from site_inspections.models.choices import WorkStatus
from site_inspections.services import common, photos

S = WorkStatus
TRANSITIONS: dict[str, frozenset[str]] = {
    S.NONE: frozenset({S.IDENTIFIED}),
    S.IDENTIFIED: frozenset({S.ENGINEERING_REVIEW}),
    S.ENGINEERING_REVIEW: frozenset({S.COST_CALCULATED}),
    S.COST_CALCULATED: frozenset({S.CUSTOMER_QUOTE_SENT}),
    S.CUSTOMER_QUOTE_SENT: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.IDENTIFIED, S.NONE}),
    S.REJECTED: frozenset({S.IDENTIFIED, S.NONE}),
}
ITEM_FIELDS = ("required", "quantity", "unit", "dimensions", "reason", "customer_impacting")
SNAPSHOT_FIELDS = ("work_type", *ITEM_FIELDS, "status", "agreement_uid")

AgreementValidator = Callable[[object, Inspection], str | None]
_validator: AgreementValidator | None = None


def register_agreement_validator(fn: AgreementValidator | None) -> AgreementValidator | None:
    """Install ``fn(agreement_uid, inspection) -> error message | None`` (the agreements context); returns the previous."""
    global _validator
    previous, _validator = _validator, fn
    return previous


# ── observations ────────────────────────────────────────────────────────────────────────────────────────────────────


def observations_queryset(inspection: Inspection):
    return Observation.objects.filter(inspection=inspection).prefetch_related("photos").order_by("created_at", "id")


@transaction.atomic
def add_observation(instance: Inspection, *, user, note: str, category: str, stage: int | None = None, photo_uids=()) -> Observation:
    inspection = common.lock(instance)
    common.begin_work(inspection, user)
    linked = [photos.get_photo(inspection, uid, field="photo_uids") for uid in dict.fromkeys(photo_uids or ())]
    observation = Observation(inspection=inspection, note=note, category=category, stage=stage)
    stamp_create(observation, user)
    observation.save()
    observation.photos.set(linked)
    common.audit("observation_added", inspection, user, after={"observation": str(observation.uid), "category": category, "stage": stage, "photos": [str(p.uid) for p in linked]})
    common.changed(inspection)
    return observation


# ── additional work ─────────────────────────────────────────────────────────────────────────────────────────────────


def items_queryset(inspection: Inspection):
    return AdditionalWorkItem.objects.filter(inspection=inspection).select_related("decided_by").order_by("work_type")


def get_item(inspection: Inspection, uid) -> AdditionalWorkItem:
    item = items_queryset(inspection).filter(uid=uid).first()
    if item is None:
        raise NotFound("not_found", "Additional-work item not found.")
    return item


@transaction.atomic
def create_item(instance: Inspection, *, user, data: dict) -> AdditionalWorkItem:
    inspection = common.lock(instance)
    common.begin_work(inspection, user)
    item = AdditionalWorkItem(inspection=inspection, work_type=data["work_type"], status=S.IDENTIFIED, **{name: data[name] for name in ITEM_FIELDS if data.get(name) is not None})
    stamp_create(item, user)
    try:
        with transaction.atomic():
            item.save()
    except IntegrityError:
        raise Conflict("work_type_exists", "This inspection already has an item of this work type.", errors={"work_type": ["Already identified."]}) from None
    common.audit("work_identified", inspection, user, after=snapshot(item, SNAPSHOT_FIELDS))
    common.changed(inspection)
    return item


def _lock_item(item: AdditionalWorkItem, expected_version) -> tuple[Inspection, AdditionalWorkItem]:
    inspection = common.lock(item.inspection)
    locked = AdditionalWorkItem.objects.select_for_update().get(pk=item.pk)
    check_version(locked, expected_version)
    return inspection, locked


@transaction.atomic
def update_item(item: AdditionalWorkItem, *, user, data: dict, expected_version=None) -> AdditionalWorkItem:
    inspection, item = _lock_item(item, expected_version)
    common.begin_work(inspection, user)
    if item.status != S.IDENTIFIED:
        raise Conflict("work_item_locked", "Only an IDENTIFIED item can be edited; move it back to IDENTIFIED first.", errors={"status": [item.status]})
    values = {name: data[name] for name in ITEM_FIELDS if name in data and getattr(item, name) != data[name]}
    if values:
        before = snapshot(item, SNAPSHOT_FIELDS)
        item.versioned_update(user, **values)
        changed_before, changed_after = changes(before, snapshot(item, SNAPSHOT_FIELDS))
        common.audit("work_updated", inspection, user, before={"work_type": item.work_type, **changed_before}, after={"work_type": item.work_type, **changed_after})
        common.changed(inspection)
    return item


@transaction.atomic
def delete_item(item: AdditionalWorkItem, *, user, expected_version=None) -> None:
    inspection, item = _lock_item(item, expected_version)
    common.begin_work(inspection, user)
    if item.status not in (S.IDENTIFIED, S.NONE):
        raise Conflict("work_item_locked", "Only an IDENTIFIED or NONE item can be removed.", errors={"status": [item.status]})
    item.soft_delete(user)
    common.audit("work_removed", inspection, user, before=snapshot(item, SNAPSHOT_FIELDS))
    common.changed(inspection)


@transaction.atomic
def transition_item(item: AdditionalWorkItem, *, user, status: str, agreement_uid=None, note: str = "", expected_version=None) -> AdditionalWorkItem:
    inspection, item = _lock_item(item, expected_version)
    if status not in TRANSITIONS.get(item.status, frozenset()):
        raise Conflict(
            "invalid_transition", f"An additional-work item cannot move from {item.status} to {status}.", errors={"status": [f"Allowed: {', '.join(sorted(TRANSITIONS[item.status])) or 'none'}."]}
        )
    engineer_move = item.status == S.NONE and status == S.IDENTIFIED
    if engineer_move:
        if not common.can_act(user, "edit"):
            raise PermissionDenied("edit_required", "Re-identifying additional work needs the site-inspection edit permission.")
        common.begin_work(inspection, user)
    else:
        common.ensure_decision_open(inspection)
        if not common.can_act(user, "approve"):
            raise PermissionDenied("approve_required", "Moving additional work past IDENTIFIED is a decision for the site-inspection approvers.")
    values: dict = {"status": status}
    if status == S.COST_CALCULATED:
        if agreement_uid is None:
            raise DomainError("agreement_required", "COST_CALCULATED needs the EXTRA_STRUCTURE agreement that prices this work.", errors={"agreement_uid": ["Required."]})
        problem = _validator(agreement_uid, inspection) if _validator is not None else None
        if problem:
            raise DomainError("agreement_invalid", problem, errors={"agreement_uid": [problem]})
        values["agreement_uid"] = agreement_uid
    if status in (S.APPROVED, S.REJECTED):
        values.update(decided_by=user, decided_at=timezone.now())
    if status in (S.IDENTIFIED, S.NONE):
        values.update(agreement_uid=None, decided_by=None, decided_at=None)
    previous = item.status
    item.versioned_update(user, **values)
    common.audit(
        "work_transition",
        inspection,
        user,
        before={"work_type": item.work_type, "status": previous},
        after={"work_type": item.work_type, "status": status, "agreement_uid": values.get("agreement_uid"), "note": note},
    )
    common.changed(inspection)
    return item
