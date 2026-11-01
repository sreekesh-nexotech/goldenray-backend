"""``change-requests/`` — submit, approve (= apply), reject, cancel.

========== ========== ============ ==============================================================================
action     from       to           notes
========== ========== ============ ==============================================================================
submit     —          PENDING      ``change_requests.create`` + view on the kind's module; the body is validated
                                   now; one pending request per row (409 ``change_request_pending``)
approve    PENDING    APPROVED     ``change_requests.approve`` + the grant applying needs; never one's own
                                   (403 ``self_approval``); applies the change as the approver in the same
                                   transaction — any refusal (``stale_version`` …) leaves the request PENDING
reject     PENDING    REJECTED     a note is required, so the requester learns why
cancel     PENDING    CANCELLED    the requester withdraws their own request
========== ========== ============ ==============================================================================

Every step is audited and emits ``approvals.change_<past>`` on the outbox.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.services.authz import can
from approvals.models import ChangeRequest
from approvals.services import kinds
from audit.services import record
from core.errors import Conflict, DomainError, NotFound, PermissionDenied, StaleVersion
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

CACHE_NAMESPACE = "approvals"
Status = ChangeRequest.Status
Operation = ChangeRequest.Operation
TARGETED = (Operation.UPDATE, Operation.DELETE)


def requests_queryset():
    return ChangeRequest.objects.select_related("created_by", "decided_by")


def _target(kind, uid):
    target = kind.model.objects.filter(uid=uid).first() if kind.model is not None else None
    if target is None:
        raise NotFound("change_target_missing", "The row this change is for no longer exists.", errors={"target_uid": ["No such row."]})
    return target


def _emit(request: ChangeRequest, past: str) -> None:
    emit(
        f"approvals.change_{past}",
        {"request_uid": str(request.uid), "kind": request.kind, "operation": request.operation, "status": request.status, "requested_by": str(request.created_by.uid) if request.created_by else None},
        aggregate_type="approvals.changerequest",
        aggregate_uid=request.uid,
        dedup_key=f"approvals.change_{past}:{request.uid}",
    )
    bump(CACHE_NAMESPACE)


@transaction.atomic
def submit(*, user, kind: str, operation: str, target_uid=None, payload=None, reason: str = "", context: dict) -> ChangeRequest:
    spec = kinds.get(kind)
    if operation not in spec.operations:
        raise DomainError("validation_error", f"{spec.label} cannot be changed that way.", errors={"operation": [f"Choose one of: {', '.join(spec.operations)}."]})
    if not can(user, spec.module, "view"):
        raise PermissionDenied("forbidden", f"Asking for a change to {spec.label} needs view access to {spec.module}.")
    payload = dict(payload or {})
    target = base_version = None
    if operation in TARGETED:
        if not target_uid:
            raise DomainError("validation_error", "Say which row to change.", errors={"target_uid": ["Required to update or delete."]})
        target = _target(spec, target_uid)
        base_version = target.version
        expected = payload.pop("expected_version", None)
        if expected not in (None, "") and int(expected) != target.version:
            raise StaleVersion()
    elif target_uid:
        raise DomainError("validation_error", "A new row has no target yet.", errors={"target_uid": ["Leave empty for this operation."]})
    if spec.prepare is not None:
        payload = spec.prepare(payload, target, context)
    spec.validate(operation, payload, target, context)
    request = ChangeRequest(
        kind=spec.key,
        operation=operation,
        target_uid=target.uid if target is not None else None,
        target_label=spec.describe(target, payload),
        payload=payload,
        before=spec.snapshot(target, payload, context),
        base_version=base_version,
        reason=(reason or "").strip(),
    )
    stamp_create(request, user)
    try:
        with transaction.atomic():
            request.save()
    except IntegrityError:
        raise Conflict("change_request_pending", "A change to this row is already waiting for approval; wait for it or ask for it to be cancelled.") from None
    record("approvals.change_requested", obj=request, actor=user, after={"kind": request.kind, "operation": request.operation, "target": request.target_label})
    _emit(request, "requested")
    return request


def _pending(instance: ChangeRequest, expected_version) -> ChangeRequest:
    request = ChangeRequest.objects.select_for_update().get(pk=instance.pk)
    check_version(request, expected_version)
    if request.status != Status.PENDING:
        raise Conflict("change_request_decided", f"This request is already {request.status.lower()}.")
    return request


@transaction.atomic
def approve(instance: ChangeRequest, *, user, note: str = "", expected_version=None, context: dict) -> ChangeRequest:
    request = _pending(instance, expected_version)
    if request.created_by_id is not None and request.created_by_id == user.pk:
        raise PermissionDenied("self_approval", "A change request has to be approved by someone else.")
    spec = kinds.get(request.kind)
    action = spec.operations[request.operation]
    if not can(user, spec.module, action):
        raise PermissionDenied("forbidden", f"Applying this change needs {spec.module}.{action}, which your role does not include.")
    target = _target(spec, request.target_uid) if request.operation in TARGETED else None
    data = spec.validate(request.operation, dict(request.payload), target, context)
    result = spec.apply(request.operation, data, target, user, request.base_version)
    request.versioned_update(
        user,
        status=Status.APPROVED,
        decided_by=user,
        decided_at=timezone.now(),
        decision_note=(note or "").strip(),
        result_uid=getattr(result, "uid", None) or request.target_uid,
    )
    record("approvals.change_approved", obj=request, actor=user, before={"status": Status.PENDING}, after={"status": Status.APPROVED, "result": str(request.result_uid or "")})
    _emit(request, "approved")
    return request


@transaction.atomic
def reject(instance: ChangeRequest, *, user, note: str, expected_version=None) -> ChangeRequest:
    note = (note or "").strip()
    if not note:
        raise DomainError("validation_error", "Say why, so the requester can fix it.", errors={"note": ["Required when rejecting."]})
    request = _pending(instance, expected_version)
    if request.created_by_id is not None and request.created_by_id == user.pk:
        raise PermissionDenied("self_approval", "Cancel your own request instead of rejecting it.")
    request.versioned_update(user, status=Status.REJECTED, decided_by=user, decided_at=timezone.now(), decision_note=note)
    record("approvals.change_rejected", obj=request, actor=user, before={"status": Status.PENDING}, after={"status": Status.REJECTED}, note=note)
    _emit(request, "rejected")
    return request


@transaction.atomic
def cancel(instance: ChangeRequest, *, user, expected_version=None) -> ChangeRequest:
    request = _pending(instance, expected_version)
    if request.created_by_id != user.pk:
        raise PermissionDenied("not_requester", "Only the person who asked can cancel a request.")
    request.versioned_update(user, status=Status.CANCELLED, decided_at=timezone.now())
    record("approvals.change_cancelled", obj=request, actor=user, before={"status": Status.PENDING}, after={"status": Status.CANCELLED})
    _emit(request, "cancelled")
    return request
