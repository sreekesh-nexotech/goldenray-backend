"""The inspection state machine (Plan 2 §3.2 table; enforced here, never by PATCHing ``status``).

=========================================  ==============================  ============================================
Transition                                 Actor (registry action)         Preconditions
=========================================  ==============================  ============================================
create → DRAFT                             ``create`` / agreements.issued  customer exists; number reserved
DRAFT / REVISION_REQUIRED → IN_PROGRESS    ``edit`` (start/ or any write)  engineer assigned
IN_PROGRESS → COMPLETED                    ``submit``                      field completion ready (includes no pending
                                                                           review, every required checklist answered)
COMPLETED → CUSTOMER_APPROVAL_PENDING      ``submit`` or ``approve``       SUITABLE/CONDITIONAL (services.approvals)
CUSTOMER_APPROVAL_PENDING → APPROVED/      the customer (OTP) or paper     latest approval PENDING, unexpired
REJECTED                                   fallback ``approve``
APPROVED → INSTALLATION_READY              ``release``                     readiness ready on the current row (D-12)
any → ON_HOLD, ON_HOLD → back              ``approve``                     reason; resume returns to the held status
COMPLETED … INSTALLATION_READY →           ``approve`` (or automatic:       supersedes the standing approvals
REVISION_REQUIRED                          system type / agreement change)
=========================================  ==============================  ============================================
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from core.errors import Conflict, DomainError
from core.outbox import emit
from engines import inspection_readiness as ir
from site_inspections.models import Inspection
from site_inspections.models.choices import Status
from site_inspections.services import common
from site_inspections.services.inspections import send_back

REVISABLE = frozenset({Status.COMPLETED, Status.CUSTOMER_APPROVAL_PENDING, Status.APPROVED, Status.INSTALLATION_READY, Status.REJECTED})


def _blocked(code: str, message: str, result: ir.Readiness) -> Conflict:
    return Conflict(code, message, errors={"blockers": [str(blocker.code) for blocker in result.blockers]})


@transaction.atomic
def submit(instance: Inspection, *, user, expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    if inspection.status != Status.IN_PROGRESS:
        raise Conflict("invalid_status", "Only an inspection in progress can be submitted.", errors={"status": [inspection.status]})
    completion = ir.field_completion(common.build_state(inspection))
    if not completion.ready:
        raise _blocked("completion_blocked", "The field inspection is not complete.", completion)
    common.set_status(inspection, user, Status.COMPLETED)
    common.changed(inspection)
    return inspection


@transaction.atomic
def hold(instance: Inspection, *, user, reason: str, expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    if inspection.status == Status.ON_HOLD:
        raise Conflict("invalid_status", "The inspection is already on hold.")
    common.ensure_decision_open(inspection)
    if not (reason or "").strip():
        raise DomainError("reason_required", "Say why the inspection is put on hold.", errors={"reason": ["Required."]})
    common.set_status(inspection, user, Status.ON_HOLD, note=reason.strip(), on_hold_reason=reason.strip(), held_from_status=inspection.status)
    common.changed(inspection)
    return inspection


@transaction.atomic
def resume(instance: Inspection, *, user, expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    if inspection.status != Status.ON_HOLD:
        raise Conflict("invalid_status", "Only an inspection on hold can be resumed.", errors={"status": [inspection.status]})
    common.set_status(inspection, user, inspection.held_from_status or Status.DRAFT, on_hold_reason="", held_from_status="")
    common.changed(inspection)
    return inspection


@transaction.atomic
def revision(instance: Inspection, *, user, reason: str, expected_version=None) -> Inspection:
    inspection = common.lock(instance, expected_version)
    if inspection.status not in REVISABLE:
        raise Conflict("invalid_status", "Only a completed, pending, approved, released or rejected inspection can be sent back.", errors={"status": [inspection.status]})
    if not (reason or "").strip():
        raise DomainError("reason_required", "Say what has to be revised.", errors={"reason": ["Required."]})
    send_back(inspection, user=user, reason=reason.strip())
    common.changed(inspection)
    return inspection


@transaction.atomic
def release(instance: Inspection, *, user, expected_version=None) -> Inspection:
    """APPROVED → INSTALLATION_READY when readiness (computed on the current row) has no blocker; emits the event."""
    inspection = common.lock(instance, expected_version)
    if inspection.status != Status.APPROVED:
        raise Conflict("invalid_status", "Only an approved inspection can be released for installation.", errors={"status": [inspection.status]})
    result = ir.evaluate(common.build_state(inspection))
    if not result.ready:
        raise _blocked("not_ready", "The inspection is not ready for installation.", result)
    now = timezone.now()
    common.set_status(inspection, user, Status.INSTALLATION_READY, released_at=now, released_by=user)
    emit(
        "site_inspections.released",
        {
            "inspection_uid": str(inspection.uid),
            "number": inspection.number,
            "customer_uid": str(inspection.customer.uid),
            "agreement_uid": str(inspection.agreement_uid) if inspection.agreement_uid else None,
            "quotation_version_uid": str(inspection.quotation_version_uid) if inspection.quotation_version_uid else None,
            "system_type": inspection.system_type,
            # projects' contract (docs/decisions/projects.md "Event contracts"): the quoted size and a project phase
            # (NC — not confirmed — is none); an inspection is not linked to a lead.
            "size_kw": str(inspection.quoted_size_kw) if inspection.quoted_size_kw is not None else None,
            "phase": inspection.phase if inspection.phase in ("1P", "3P") else None,
            "lead_uid": None,
            "released_at": now.isoformat(),
            "released_by_uid": str(user.uid) if getattr(user, "uid", None) else None,
        },
        aggregate_type=common.OBJECT_TYPE,
        aggregate_uid=inspection.uid,
        dedup_key=f"site_inspections.released:{inspection.uid}:{inspection.version}",
    )
    common.changed(inspection)
    return inspection
