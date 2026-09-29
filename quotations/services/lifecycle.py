"""Quotation lifecycle after issue: accept, cancel, expire (Beat), discount requests, history, document links.

* ``accept`` (ISSUED and still valid) → ACCEPTED and ``quotations.accepted`` (agreements drafts the PA, PLAN §3.5);
* ``cancel`` (DRAFT/ISSUED/EXPIRED, a reason required) → CANCELLED, ``quotations.cancelled``;
* ``expire_due`` — ISSUED quotations whose ``valid_until`` (the validity policy frozen at issue) has passed become
  EXPIRED (``quotations.expired``), run daily by ``quotations.tasks.expire_quotations``;
* discount requests on the DRAFT version: PENDING → APPROVED (adds to the version's ``discount_total``, printed at
  issue, D-4) or REJECTED by a ``quotations.approve`` holder who is not the requester (``deny_self_action``).
"""

from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.services.authz import deny_self_action
from audit.models import AuditLog
from audit.services import record
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from quotations.models import DiscountRequest, DiscountStatus, Quotation, QuotationStatus, Version, VersionStatus
from quotations.services.common import CACHE_NAMESPACE, money

HISTORY_LIMIT = 200


def _lock_quotation(quotation: Quotation, expected_version=None) -> Quotation:
    locked = Quotation.objects.select_for_update().filter(pk=quotation.pk).first()
    if locked is None:
        raise NotFound("not_found", "Quotation not found.")
    check_version(locked, expected_version)
    return locked


def _event(name: str, quotation: Quotation, **extra) -> None:
    emit(
        f"quotations.{name}",
        {"quotation_uid": str(quotation.uid), "customer_uid": str(quotation.customer.uid), "number": quotation.number, **extra},
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
    )


@transaction.atomic
def accept(quotation: Quotation, *, user, note: str = "", expected_version=None) -> Quotation:
    quotation = _lock_quotation(quotation, expected_version)
    if quotation.status != QuotationStatus.ISSUED:
        raise Conflict("quotation_not_issued", f"Only an ISSUED quotation can be accepted (this one is {quotation.status}).")
    if quotation.valid_until is not None and quotation.valid_until < timezone.localdate():
        raise Conflict("quotation_expired", f"The quotation expired on {quotation.valid_until.isoformat()}; revise it first.")
    version = quotation.current_version
    now = timezone.now()
    quotation.versioned_update(user, status=QuotationStatus.ACCEPTED, accepted_at=now)
    record("quotations.accepted", obj=quotation, actor=user, after={"version": version.number, "final_price": str(version.final_price), "note": note})
    _event("accepted", quotation, version_uid=str(version.uid), version=version.number, final_price=str(version.final_price), language=version.language, accepted_at=now.isoformat())
    bump(CACHE_NAMESPACE)
    return quotation


@transaction.atomic
def cancel(quotation: Quotation, *, user, reason: str, expected_version=None) -> Quotation:
    quotation = _lock_quotation(quotation, expected_version)
    if quotation.status not in (QuotationStatus.DRAFT, QuotationStatus.ISSUED, QuotationStatus.EXPIRED):
        raise Conflict("quotation_closed", f"A {quotation.status} quotation cannot be cancelled.")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["Required."]})
    previous = quotation.status
    quotation.versioned_update(user, status=QuotationStatus.CANCELLED, cancelled_at=timezone.now(), lost_reason=reason)
    record("quotations.cancelled", obj=quotation, actor=user, before={"status": previous}, after={"status": quotation.status, "reason": reason})
    _event("cancelled", quotation, reason=reason, previous_status=previous)
    bump(CACHE_NAMESPACE)
    return quotation


def expire_due(today: dt.date | None = None) -> list[str]:
    """ISSUED quotations past their validity become EXPIRED (one transaction per quotation; skips locked rows)."""
    today = today or timezone.localdate()
    expired = []
    for pk in list(Quotation.objects.filter(status=QuotationStatus.ISSUED, valid_until__lt=today).values_list("pk", flat=True)):
        with transaction.atomic():
            quotation = Quotation.objects.select_for_update(skip_locked=True, of=("self",)).filter(pk=pk, status=QuotationStatus.ISSUED, valid_until__lt=today).select_related("customer").first()
            if quotation is None:
                continue
            quotation.versioned_update(None, status=QuotationStatus.EXPIRED, expired_at=timezone.now())
            record("quotations.expired", obj=quotation, actor=None, after={"valid_until": quotation.valid_until.isoformat()})
            _event("expired", quotation, valid_until=quotation.valid_until.isoformat())
            expired.append(quotation.number)
    if expired:
        bump(CACHE_NAMESPACE)
    return expired


# ── discount requests ───────────────────────────────────────────────────────────────────────────────────────────


def draft_version(quotation: Quotation) -> Version:
    version = Version.objects.filter(quotation=quotation, status=VersionStatus.DRAFT).first()
    if version is None:
        raise Conflict("no_draft", "The quotation has no DRAFT version; revise it first.")
    return version


@transaction.atomic
def request_discount(quotation: Quotation, *, user, amount, reason: str) -> DiscountRequest:
    version = Version.objects.select_for_update().get(pk=draft_version(quotation).pk)
    amount = money(amount)
    if amount is None or amount <= 0:
        raise DomainError("validation_error", "The amount must be positive.", errors={"amount": ["Must be greater than 0."]})
    if not (reason or "").strip():
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["Required."]})
    request = DiscountRequest(quotation_version=version, requested_by=user if getattr(user, "pk", None) else None, amount=amount, reason=reason.strip())
    stamp_create(request, user)
    try:
        with transaction.atomic():
            request.save()
    except IntegrityError:
        raise Conflict("discount_pending", "A discount request is already pending on this draft.") from None
    record("quotations.discount_requested", obj=quotation, actor=user, after={"request": str(request.uid), "amount": str(amount), "version": version.number})
    emit(
        "quotations.discount_requested",
        {"quotation_uid": str(quotation.uid), "request_uid": str(request.uid), "amount": str(amount)},
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
    )
    bump(CACHE_NAMESPACE)
    return request


def get_request(quotation: Quotation, uid) -> DiscountRequest:
    request = DiscountRequest.objects.select_related("quotation_version", "requested_by", "decided_by").filter(quotation_version__quotation=quotation, uid=uid).first()
    if request is None:
        raise NotFound("not_found", "Discount request not found.")
    return request


@transaction.atomic
def decide_discount(request: DiscountRequest, *, user, approve: bool, note: str = "", expected_version=None) -> DiscountRequest:
    request = DiscountRequest.objects.select_for_update(of=("self",)).select_related("quotation_version__quotation", "requested_by").filter(pk=request.pk).first()
    if request is None:
        raise NotFound("not_found", "Discount request not found.")
    check_version(request, expected_version)
    if request.status != DiscountStatus.PENDING:
        raise Conflict("discount_decided", f"The request is already {request.status}.")
    if request.requested_by is not None:
        deny_self_action(user, request.requested_by, message="You cannot decide your own discount request.")
    version = Version.objects.select_for_update().get(pk=request.quotation_version_id)
    if version.status != VersionStatus.DRAFT:
        raise Conflict("version_not_draft", "The version was issued or superseded; the request can no longer be decided.")
    status = DiscountStatus.APPROVED if approve else DiscountStatus.REJECTED
    request.versioned_update(user, status=status, decided_by=user if getattr(user, "pk", None) else None, decided_at=timezone.now(), note=note or "")
    if approve:
        version.versioned_update(user, discount_total=money(version.discount_total + request.amount))
    quotation = version.quotation
    record(f"quotations.discount_{status.lower()}", obj=quotation, actor=user, after={"request": str(request.uid), "amount": str(request.amount), "discount_total": str(version.discount_total)})
    emit(
        f"quotations.discount_{status.lower()}",
        {"quotation_uid": str(quotation.uid), "request_uid": str(request.uid), "amount": str(request.amount)},
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
    )
    bump(CACHE_NAMESPACE)
    return request


# ── history and documents ───────────────────────────────────────────────────────────────────────────────────────


def history(quotation: Quotation) -> dict:
    """The versions (newest first) and the quotation's audit trail (newest first, at most 200 entries)."""
    versions = list(Version.objects.filter(quotation=quotation).select_related("issued_by").order_by("-number"))
    events = AuditLog.objects.filter(object_type="quotations.quotation", object_uid=quotation.uid).select_related("actor").order_by("-at", "-id")[:HISTORY_LIMIT]
    return {"versions": versions, "events": list(events)}


def document_job(version: Version, language: str):
    if version.status == VersionStatus.DRAFT:
        raise Conflict("version_not_issued", "A draft has no document; preview it instead.")
    job = version.document_job_ml if language == "ml" else version.document_job
    if job is None:
        raise Conflict("document_not_ready", "This version has no rendered document (legacy versions are rendered on request).")
    return job


@transaction.atomic
def rerender(version: Version, *, user, language: str):
    """Render an issued version's frozen payload again (a legacy version, or a failed job): never re-derived."""
    from documents.services.jobs import request_render

    version = Version.objects.select_for_update().get(pk=version.pk)
    if version.status == VersionStatus.DRAFT or not version.document_payload:
        raise Conflict("version_not_issued", "Only an issued version with a frozen document can be rendered.")
    job = request_render("QUOTATION", "quotations.version", version.uid, "default", language, version.document_payload, user, reuse=True)
    field = "document_job_ml" if language == "ml" else "document_job"
    if getattr(version, f"{field}_id") != job.pk:
        version.versioned_update(user, **{field: job})
    record("quotations.document_rendered", obj=version.quotation, actor=user, after={"version": version.number, "language": language, "payload_sha256": job.payload_sha256})
    return job
