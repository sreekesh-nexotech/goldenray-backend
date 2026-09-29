"""Issue an agreement, render it, hand out its document (PLAN §3.4 Agreements; Plan 2 §3.1 "Issue").

``issue`` (DRAFT → ISSUED, all or nothing):

1. the fields the Purchase Agreement page's form of that kind asked for must be present (``FORMS`` 1/2/3; 422
   ``agreement_incomplete`` listing each missing field, nothing written);
2. a quotation-derived draft must still point at the quotation's ISSUED version and an open quotation;
3. a superseding draft replaces an agreement still in force, which becomes SUPERSEDED in the same transaction;
4. the number comes from ``core.sequences`` AGR (``AGR-<FY>-<nnnn>``, D-11) inside the transaction;
5. the document (letterhead, payee and bank from the company master) is deep-frozen into ``payload`` with its
   canonical SHA-256 and rendered in the agreement's language;
6. ``agreements.issued`` (and ``agreements.superseded`` for a revision) carries the site-inspection contract payload.

``render`` renders the frozen document again in any language (a DRAFT: its current, unfrozen document); it never
changes the payload. ``document_job`` finds the rendered PDF of a language for the signed download link.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from agreements.models import Agreement, AgreementKind, AgreementStatus
from agreements.services import document
from agreements.services.common import CACHE_NAMESPACE, OBJECT_TYPE, agreement_snapshot, lock, require_draft
from audit.services import record
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.sequences import next_number
from engines.frozen import sha256_hex
from flarize.cache_utils import bump

KIND = "AGREEMENT"
PRICE_FIELDS = {AgreementKind.PURCHASE_AGREEMENT: ("original_price", "final_price"), AgreementKind.SALE_ORDER: ("original_price",), AgreementKind.EXTRA_STRUCTURE: ("original_price",)}
# The page's FORMS field sets (1: Purchase Agreement, 2: Sale Order, 3: Extra Structure), in platform columns.
REQUIRED = {
    AgreementKind.PURCHASE_AGREEMENT: ("capacity_kw", "phase", "panel_label", "inverter_brand", "inverter_type", "structure_material", "variant"),
    AgreementKind.SALE_ORDER: ("capacity_kw", "phase", "panel_label", "inverter_brand", "inverter_type", "variant"),
    AgreementKind.EXTRA_STRUCTURE: ("capacity_kw", "phase", "panel_label", "inverter_brand", "inverter_type", "variant"),
}
CUSTOMER_REQUIRED = {AgreementKind.PURCHASE_AGREEMENT: ("name", "phone_e164", "address"), AgreementKind.SALE_ORDER: ("name",), AgreementKind.EXTRA_STRUCTURE: ("name",)}


def missing_fields(agreement: Agreement) -> dict[str, list[str]]:
    errors = {}
    for name in REQUIRED[agreement.kind] + PRICE_FIELDS[agreement.kind]:
        if getattr(agreement, name) in (None, ""):
            errors[name] = ["Required to issue."]
    for name in CUSTOMER_REQUIRED[agreement.kind]:
        if not getattr(agreement.customer, name):
            errors[f"customer.{name}"] = ["Required to issue (edit the customer)."]
    if agreement.kind == AgreementKind.EXTRA_STRUCTURE and not agreement.extra_cost:
        errors["lines"] = ["An Extra Structure agreement needs priced lines."]
    return errors


def _require_quotation_current(agreement: Agreement) -> None:
    version = agreement.quotation_version
    if version is None:
        return
    if version.status != "ISSUED":
        raise Conflict("quotation_version_superseded", f"Quotation version {version.number} is {version.status}; supersede this draft onto the current version.")
    if version.quotation.status not in ("ISSUED", "ACCEPTED"):
        raise Conflict("quotation_closed", f"The quotation is {version.quotation.status}.")


def _render(agreement: Agreement, payload: dict, language: str, user):
    from documents.services.jobs import request_render

    return request_render(KIND, OBJECT_TYPE, agreement.uid, "default", language, payload, user, reuse=True)


@transaction.atomic
def issue(instance: Agreement, *, user, expected_version=None) -> Agreement:
    agreement = lock(instance, expected_version)
    require_draft(agreement)
    agreement = Agreement.objects.select_related("customer", "customer__lead", "quotation_version", "quotation_version__quotation", "supersedes", "panel", "inverter", "battery").get(pk=agreement.pk)
    errors = missing_fields(agreement)
    if errors:
        raise DomainError("agreement_incomplete", "The agreement is missing fields its document needs.", status=422, errors=errors)
    _require_quotation_current(agreement)
    previous = None
    if agreement.supersedes_id is not None:
        previous = lock(agreement.supersedes)
        if previous.status not in (AgreementStatus.ISSUED, AgreementStatus.ACCEPTED):
            raise Conflict("supersedes_not_in_force", f"The agreement this revision replaces is {previous.status}.")
    now = timezone.now()
    if previous is not None:
        previous.versioned_update(user, status=AgreementStatus.SUPERSEDED, superseded_at=now)
    agreement.number = next_number("AGR")
    agreement.issued_at = now
    payload = document.build(agreement)
    try:
        with transaction.atomic():
            agreement.versioned_update(
                user,
                status=AgreementStatus.ISSUED,
                number=agreement.number,
                issued_at=now,
                issued_by=user if getattr(user, "pk", None) else None,
                payload=payload,
                payload_sha256=sha256_hex(payload),
            )
    except IntegrityError:
        raise Conflict("agreement_exists", "Another agreement of this kind is already issued for this quotation version.") from None
    job = _render(agreement, payload, agreement.language, user)
    agreement.versioned_update(user, document_job=job)
    record("agreements.issued", obj=agreement, actor=user, after={**agreement_snapshot(agreement), "payload_sha256": agreement.payload_sha256, "supersedes": str(previous.uid) if previous else None})
    event = document.event_payload(agreement)
    emit("agreements.issued", event, aggregate_type=OBJECT_TYPE, aggregate_uid=agreement.uid, dedup_key=f"agreements.issued:{agreement.uid}")
    if previous is not None:
        record("agreements.superseded", obj=previous, actor=user, after={"status": previous.status, "superseded_by": str(agreement.uid), "superseded_by_number": agreement.number})
        emit("agreements.superseded", event, aggregate_type=OBJECT_TYPE, aggregate_uid=agreement.uid, dedup_key=f"agreements.superseded:{agreement.uid}")
    bump(CACHE_NAMESPACE)
    return agreement


def current_payload(agreement: Agreement) -> dict:
    """The frozen document of an issued agreement, else the DRAFT's document as it stands (unfrozen)."""
    if agreement.payload is not None:
        return agreement.payload
    if agreement.status == AgreementStatus.DRAFT:
        return document.build(agreement)
    raise Conflict("agreement_not_issued", "This agreement was cancelled before it was issued; it has no document.")


@transaction.atomic
def render(instance: Agreement, *, user, language: str):
    agreement = lock(instance)
    job = _render(agreement, current_payload(agreement), language, user)
    record("agreements.document_rendered", obj=agreement, actor=user, after={"language": language, "payload_sha256": job.payload_sha256, "status": agreement.status})
    return job


def document_job(agreement: Agreement, language: str):
    """The newest usable rendering of the agreement's current document in ``language`` (409 while there is none): the
    frozen payload of an issued agreement, the DRAFT's document as it stands now (an edit needs a new render)."""
    from documents.models import RenderJob
    from documents.services.jobs import canonical_payload

    jobs = RenderJob.objects.filter(object_type=OBJECT_TYPE, object_uid=agreement.uid, language=language).exclude(status=RenderJob.Status.FAILED)
    if agreement.payload is not None:
        digest = agreement.payload_sha256
    else:
        digest = canonical_payload(current_payload(Agreement.objects.select_related("customer").get(pk=agreement.pk)))[1]
    jobs = jobs.filter(payload_sha256=digest)
    job = jobs.order_by("-created_at", "-id").first()
    if job is None:
        raise Conflict("document_not_ready", f"No {language} document has been rendered; render it first.", errors={"language": [language]})
    return job
