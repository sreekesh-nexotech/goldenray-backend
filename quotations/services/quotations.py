"""Quotation lifecycle (PLAN §2.6, §3.4 Sales; workflows spec C.1): create, edit the draft, preview, issue, revise.

* ``create`` — a quotation for a customer the user can see, with its first DRAFT version pinned to the current
  PackRelease (+ its PriceRelease) and the published ContentRelease; the primary pack must be released
  (409 ``no_approved_package``).
* ``update_draft`` — PATCH of the DRAFT's selections (``expected_version``); ``refresh_release`` re-pins a draft whose
  PackRelease was superseded.
* ``preview`` — the payload and the 8-check gate report, nothing written.
* ``issue`` — the gate must pass (422 ``generation_blocked``, nothing written); the number comes from
  ``core.sequences`` QUO (continuing the Flarize counter) at the first issue; BOM and commercial snapshots per tier,
  the engineering run, the deep-frozen document + SHA-256, en/ml renders, ``quotations.issued``.
* ``revise`` — an ISSUED/EXPIRED quotation gets a new DRAFT version from the current releases; the issued version
  becomes SUPERSEDED (Flarize ``createRevision``).

Accept, cancel, expire, history and documents are in :mod:`quotations.services.lifecycle`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.scopes import apply as apply_scope
from core.sequences import next_number
from core.services import check_version, stamp_create
from engines.frozen import sha256_hex
from engines.quotation_payload import PolicyError, project_issued_payload_for_actor, resolve_effective_validity
from flarize.cache_utils import bump
from quotations.models import BomSnapshot, CommercialSnapshot, DiscountRequest, DiscountStatus, Quotation, QuotationSource, QuotationStatus, Version, VersionStatus
from quotations.services import inputs, pipeline
from quotations.services.common import CACHE_NAMESPACE, ENGINE_TIER, PLATFORM_TIER, can_see_internal, fraction, iso, money, plain

VERSION_SNAPSHOT_FIELDS = ("status", "system_type", "tier", "size_key", "phase", "battery_config", "future_size_key", "roof_type", "distance_km", "vehicle_type", "subsidy_type", "language")


def quotations_queryset():
    return Quotation.objects.select_related("customer", "owner", "current_version", "current_version__pack_release")


def versions_queryset():
    return Version.objects.select_related("quotation", "quotation__customer", "pack_release", "price_release", "content_release", "issued_by", "document_job", "document_job_ml")


def get_version(quotation: Quotation, number: int) -> Version:
    version = versions_queryset().filter(quotation=quotation, number=number).first()
    if version is None:
        raise NotFound("version_not_found", f"Quotation has no version {number}.")
    return version


def _customer_for(user, customer_uid):
    from customers.models import Customer

    customer = apply_scope(Customer.objects.all(), user, "customers").filter(uid=customer_uid).first()
    if customer is None:
        raise DomainError("validation_error", "Unknown customer.", errors={"customer_uid": ["No customer you can see has this uid."]})
    return customer


def _classification(data: dict) -> dict:
    source = data.get("source") or QuotationSource.DIRECT
    if source not in QuotationSource.values:
        raise DomainError("validation_error", "Unknown source.", errors={"source": ["DIRECT, AFFILIATE or DISTRICT."]})
    affiliate = (data.get("affiliate_ref") or "").strip()
    district = (data.get("district") or "").strip()
    if affiliate and source != QuotationSource.AFFILIATE:
        raise DomainError("validation_error", "affiliate_ref is only allowed with source AFFILIATE.", errors={"affiliate_ref": ["Only for AFFILIATE quotations."]})
    if source == QuotationSource.DISTRICT and not district:
        raise DomainError("validation_error", "A DISTRICT quotation needs the district.", errors={"district": ["Required for DISTRICT."]})
    return {"source": source, "affiliate_ref": affiliate[:64], "district": district[:100]}


@transaction.atomic
def create(*, user, data: dict) -> Quotation:
    customer = _customer_for(user, data.get("customer_uid"))
    release = inputs.current_pack_release()
    fields = inputs.resolve_fields(data, release=release, user=user)
    quotation = Quotation(customer=customer, owner=user if getattr(user, "pk", None) else None, status=QuotationStatus.DRAFT, **_classification(data))
    stamp_create(quotation, user)
    quotation.save()
    version = Version(quotation=quotation, number=1, status=VersionStatus.DRAFT, pack_release=release, price_release=release.price_release, content_release=inputs.current_content_release(), **fields)
    stamp_create(version, user)
    version.save()
    Quotation.objects.filter(pk=quotation.pk).update(current_version=version)
    quotation.current_version = version
    record("quotations.created", obj=quotation, actor=user, after={"customer": str(customer.uid), "pack_release": release.number, **_version_audit(version)})
    emit(
        "quotations.created",
        {"quotation_uid": str(quotation.uid), "customer_uid": str(customer.uid), "owner_uid": str(user.uid) if getattr(user, "pk", None) else None},
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
    )
    bump(CACHE_NAMESPACE)
    return quotation


def _version_audit(version: Version) -> dict:
    return {name: str(getattr(version, name)) for name in VERSION_SNAPSHOT_FIELDS}


def lock_version(version: Version, expected_version=None) -> Version:
    locked = Version.objects.select_for_update().filter(pk=version.pk).first()
    if locked is None:
        raise NotFound("version_not_found", "Version not found.")
    check_version(locked, expected_version)
    return locked


def _require_draft(version: Version) -> None:
    if version.status != VersionStatus.DRAFT:
        raise Conflict("version_not_draft", "Only the DRAFT version can be changed or issued.", errors={"status": [version.status]})


def _require_release(version: Version) -> None:
    """An imported (legacy) draft predates releases: it is priced only after ``refresh_release`` pins it."""
    if version.pack_release_id is None or version.price_release_id is None:
        raise Conflict("release_required", "This draft is not pinned to a PackRelease (imported draft); refresh it first (PATCH refresh_release).")


def _require_open(quotation: Quotation) -> None:
    if quotation.status in (QuotationStatus.ACCEPTED, QuotationStatus.CANCELLED):
        raise Conflict("quotation_closed", f"The quotation is {quotation.status}.")


@transaction.atomic
def update_draft(version: Version, *, user, data: dict, expected_version=None) -> Version:
    version = lock_version(version, expected_version)
    _require_draft(version)
    _require_open(Quotation.objects.get(pk=version.quotation_id))
    release = version.pack_release
    repinned = {}
    if data.pop("refresh_release", False):
        release = inputs.current_pack_release()
        repinned = {"pack_release": release, "price_release": release.price_release, "content_release": inputs.current_content_release(), "notices": [], "legacy": False}
    else:
        _require_release(version)
    fields = inputs.resolve_fields(data, release=release, user=user, base=inputs.version_inputs(version))
    changed = {name: value for name, value in fields.items() if getattr(version, name) != value}
    changed.update({name: value for name, value in repinned.items() if getattr(version, name) != value})
    if changed:
        before = _version_audit(version)
        version.versioned_update(user, **changed)
        record("quotations.draft_updated", obj=version.quotation, actor=user, before=before, after={**_version_audit(version), "fields": sorted(changed)})
        bump(CACHE_NAMESPACE)
    return version


# ── pricing through the pipeline ──────────────────────────────────────────────────────────────────────────────────


def _quotation_section(quotation: Quotation, version: Version, *, number: str, at: str, valid_until: str | None, user) -> dict:
    owner = quotation.owner or user
    return {
        "quotationId": str(quotation.uid),
        "quotationNumber": number,
        "quotationDate": at,
        "validUntil": valid_until,
        "proposalBy": inputs.display_name(owner) or None,
        "salespersonId": str(owner.uid) if owner is not None and getattr(owner, "pk", None) else None,
        "quotationLanguage": version.language,
        "status": "DRAFT",
        "quotationSource": quotation.source,
        "district": quotation.district or quotation.customer.district or None,
        "affiliateId": quotation.affiliate_ref or None,
    }


def _validity(policy, override, at: str) -> dict:
    try:
        return plain(resolve_effective_validity(policy, override, at))
    except PolicyError as error:
        if error.code == "POLICY_NOT_CONFIGURED":
            raise Conflict("policy_not_configured", "No quotation validity policy is configured (pricing/validity-policy/).") from None
        raise DomainError(str(error.code).lower(), error.message if hasattr(error, "message") else str(error)) from None


def _peek_number() -> str:
    from core.models import SequenceCounter

    counter = SequenceCounter.objects.filter(kind="QUO", period_key="").first()
    return f"GR-{counter.next_value if counter else 1}"


def _run(version: Version, *, user, freeze: bool, number: str, at: str, content_release):
    quotation = version.quotation
    request = pipeline.Request.from_version(version)
    policy = inputs.policy_store()
    validity = _validity(policy, request.validity_override_days, at) if freeze else (_try_validity(policy, request.validity_override_days, at))
    ctx = pipeline.ReleaseContext(version.pack_release)
    return pipeline.build(
        ctx,
        request,
        quotation=_quotation_section(quotation, version, number=number, at=at, valid_until=(validity or {}).get("validUntil"), user=user),
        customer=inputs.customer_document(quotation.customer),
        at=at,
        actor_id=str(user.uid) if getattr(user, "pk", None) else "system",
        actor_role=getattr(getattr(user, "role", None), "slug", "") or "",
        version_uid=version.uid,
        version_number=version.number,
        content_release=content_release,
        company=inputs.company_document(),
        branding_store=inputs.branding_store(),
        policy=policy,
        freeze=freeze,
    )


def _try_validity(policy, override, at):
    try:
        return plain(resolve_effective_validity(policy, override, at)) if policy else None
    except PolicyError:
        return None


def pricing_summary(outcome, *, internal: bool) -> dict:
    tiers = {}
    for tier, result in outcome.tiers.items():
        customer = result.pack_pricing.get("customer") or {}
        entry = {
            "pack_key": result.release_pack.key,
            "selling_price_before_gst": customer.get("sellingPriceBeforeGST"),
            "gst_amount": customer.get("gstAmount"),
            "selling_price_including_gst": customer.get("sellingPriceIncludingGST"),
            "transport_extra": customer.get("transportExtra"),
            "customer_total_including_gst": customer.get("customerTotalIncludingGST"),
            "offer_amount": str(result.offer_amount),
            "engineering_status": result.check.status.value,
        }
        if internal:
            entry["internal"] = plain(result.pack_pricing.get("internal"))
        tiers[PLATFORM_TIER[tier]] = entry
    return {"tiers": tiers, "unavailable": outcome.unavailable}


def preview(version: Version, *, user) -> dict:
    """``buildPayload`` of the DRAFT: the payload (projected for the reader) and the gate report; nothing is written."""
    _require_draft(version)
    _require_release(version)
    at = iso()
    number = version.quotation.number or _peek_number()
    outcome = _run(version, user=user, freeze=False, number=number, at=at, content_release=inputs.current_content_release())
    internal = can_see_internal(user)
    return {
        "gate_report": plain(outcome.gate.as_dict()),
        "payload": plain(project_issued_payload_for_actor(outcome.payload, internal)),
        "pricing": pricing_summary(outcome, internal=internal),
        "at": at,
    }


# ── issue ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def _reference_margin(result) -> Decimal | None:
    customer = result.pack_pricing.get("customer") or {}
    internal = result.pack_pricing.get("internal") or {}
    selling, cost = customer.get("sellingPriceBeforeGST"), internal.get("referenceTotal")
    if not selling or cost is None:
        return None
    return fraction((Decimal(str(selling)) - Decimal(str(cost))) / Decimal(str(selling)))


def _write_snapshots(version: Version, outcome, *, user, run) -> None:
    for tier, result in outcome.tiers.items():
        primary = result is outcome.primary
        bom = BomSnapshot(
            quotation_version=version,
            tier=PLATFORM_TIER[tier],
            is_primary=primary,
            lines=plain(result.lock.get("lines") or []),
            lock_acknowledgements=plain(result.lock.get("acknowledgements") or []),
            engineering_run=run,
            engineering_status=result.check.status.value,
            record=plain(result.lock),
        )
        stamp_create(bom, user)
        bom.save()
        snapshot = plain(result.commercial_snapshot)
        cost = snapshot.get("cost") or {}
        commercial = CommercialSnapshot(
            quotation_version=version,
            tier=PLATFORM_TIER[tier],
            is_primary=primary,
            cost_lines={name: snapshot.get(name) for name in ("cost", "pricing", "pack", "offer")},
            pins={
                **(snapshot.get("versions") or {}),
                "packRelease": version.pack_release.number,
                "priceRelease": version.price_release.number,
                "contentRelease": version.content_release.number if version.content_release_id else None,
                "rulesVersion": result.check.rules_version,
            },
            margin_check={"landedCostCheck": cost.get("landedCostCheck"), "referenceMarginPct": cost.get("referenceMarginPct"), "grossMarginFraction": str(_reference_margin(result))},
            customer_total_incl_gst=money((snapshot.get("pricing") or {}).get("customerTotalIncludingGST")),
            record=snapshot,
        )
        stamp_create(commercial, user)
        commercial.save()


def _record_run(version: Version, outcome, *, user):
    from engineering.models import SubjectType
    from engineering.services import runs

    ctx_rule_set = pipeline.ReleaseContext(version.pack_release).rule_set_row
    checks = [(PLATFORM_TIER[tier], result.check) for tier, result in outcome.tiers.items()]
    label = f"{version.quotation.number or version.quotation.uid} v{version.number}"
    return runs.record_run(rule_set=ctx_rule_set, subject_type=SubjectType.QUOTATION_DRAFT, subject_uid=version.uid, subject_label=label, checks=checks, user=user)


def _render(version: Version, *, user) -> tuple:
    from documents.services.jobs import request_render

    jobs = []
    for language in ("en", "ml"):
        jobs.append(request_render("QUOTATION", "quotations.version", version.uid, "default", language, version.document_payload, user))
    return tuple(jobs)


def _local_date(iso_value: str | None) -> dt.date | None:
    if not iso_value:
        return None
    moment = dt.datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    return timezone.localtime(moment).date()


@transaction.atomic
def issue(version: Version, *, user, expected_version=None) -> Version:
    quotation = Quotation.objects.select_for_update().filter(pk=version.quotation_id).first()
    if quotation is None:
        raise NotFound("not_found", "Quotation not found.")
    version = lock_version(version, expected_version)
    _require_draft(version)
    _require_open(quotation)
    _require_release(version)
    current = inputs.current_pack_release()
    if version.pack_release_id != current.pk:
        raise Conflict("release_superseded", f"PackRelease #{version.pack_release.number} was superseded by #{current.number}; refresh the draft (PATCH refresh_release).")
    if DiscountRequest.objects.filter(quotation_version=version, status=DiscountStatus.PENDING).exists():
        raise Conflict("discount_pending", "A discount request is pending; approve or reject it first.")
    content_release = inputs.current_content_release()
    number = quotation.number or next_number("QUO")
    at = iso()
    outcome = _run(version, user=user, freeze=True, number=number, at=at, content_release=content_release)
    if not outcome.gate.passed:
        raise DomainError(
            "generation_blocked",
            f"Quotation generation disabled. Failed checks: {', '.join(check.value for check in outcome.gate.blocked_checks)}.",
            status=422,
            errors={"gate": list(outcome.gate.reasons)},
        )
    primary = outcome.primary
    customer = primary.pack_pricing["customer"]
    total = Decimal(str(customer["customerTotalIncludingGST"]))
    reduction = primary.offer_amount + (version.discount_total or Decimal("0"))
    if reduction > total:
        raise Conflict("reduction_exceeds_price", "The offer and approved discounts exceed the customer price.")
    document = plain(outcome.document)
    run = _record_run(version, outcome, user=user)
    _write_snapshots(version, outcome, user=user, run=run)
    now = timezone.now()
    version.versioned_update(
        user,
        status=VersionStatus.ISSUED,
        content_release=content_release,
        customer_price_incl_gst=money(customer["sellingPriceIncludingGST"]),
        transport_extra=money(customer.get("transportExtra") or 0),
        offer_total=money(primary.offer_amount),
        final_price=money(total - reduction),
        gross_margin_pct=_reference_margin(primary),
        gate_report=plain(outcome.gate.as_dict()),
        issued_at=now,
        issued_by=user if getattr(user, "pk", None) else None,
        document_payload=document,
        document_payload_sha256=sha256_hex(outcome.document),
    )
    job_en, job_ml = _render(version, user=user)
    version.versioned_update(user, document_job=job_en, document_job_ml=job_ml)
    valid_until = _local_date((outcome.validity or {}).get("validUntil"))
    values = {"status": QuotationStatus.ISSUED, "number": number, "current_version": version, "valid_until": valid_until, "expired_at": None}
    if quotation.issued_at is None:
        values["issued_at"] = now
    try:
        with transaction.atomic():
            quotation.versioned_update(user, **values)
    except IntegrityError:
        raise Conflict("number_taken", f"Quotation number {number} is already used.") from None
    record(
        "quotations.issued",
        obj=quotation,
        actor=user,
        after={"number": number, "version": version.number, "final_price": str(version.final_price), "document_sha256": version.document_payload_sha256, "unavailable": outcome.unavailable},
    )
    emit(
        "quotations.issued",
        {
            "quotation_uid": str(quotation.uid),
            "customer_uid": str(quotation.customer.uid),
            "version_uid": str(version.uid),
            "number": number,
            "version": version.number,
            "final_price": str(version.final_price),
            "language": version.language,
        },
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
        dedup_key=f"quotations.issued:{version.uid}",
    )
    bump(CACHE_NAMESPACE)
    return version


# ── revise ──────────────────────────────────────────────────────────────────────────────────────────────────────────


@transaction.atomic
def revise(quotation: Quotation, *, user, data: dict | None = None, expected_version=None) -> Version:
    quotation = Quotation.objects.select_for_update().filter(pk=quotation.pk).first()
    if quotation is None:
        raise NotFound("not_found", "Quotation not found.")
    check_version(quotation, expected_version)
    if quotation.status not in (QuotationStatus.ISSUED, QuotationStatus.EXPIRED):
        raise Conflict("quotation_not_issued", f"Only an ISSUED or EXPIRED quotation is revised (this one is {quotation.status}); a DRAFT is edited.")
    current = Version.objects.select_for_update().filter(pk=quotation.current_version_id).first()
    if current is None:
        raise Conflict("quotation_not_issued", "The quotation has no issued version.")
    release = inputs.current_pack_release()
    base = inputs.version_inputs(current)
    fields = inputs.resolve_fields(data or {}, release=release, user=user, base=base)
    now = timezone.now()
    if current.status == VersionStatus.ISSUED:
        current.versioned_update(user, status=VersionStatus.SUPERSEDED, superseded_at=now)
    number = (Version.all_objects.filter(quotation=quotation).aggregate(top=Max("number"))["top"] or 0) + 1
    draft = Version(
        quotation=quotation,
        number=number,
        status=VersionStatus.DRAFT,
        pack_release=release,
        price_release=release.price_release,
        content_release=inputs.current_content_release(),
        **fields,
    )
    stamp_create(draft, user)
    draft.save()
    quotation.versioned_update(user, status=QuotationStatus.DRAFT, current_version=draft, expired_at=None)
    record("quotations.revised", obj=quotation, actor=user, after={"superseded_version": current.number, "draft_version": number, **_version_audit(draft)})
    emit(
        "quotations.revised",
        {"quotation_uid": str(quotation.uid), "superseded_version_uid": str(current.uid), "draft_version_uid": str(draft.uid), "number": quotation.number},
        aggregate_type="quotations.quotation",
        aggregate_uid=quotation.uid,
    )
    bump(CACHE_NAMESPACE)
    return draft


def payload_for_reader(version: Version, user) -> dict | None:
    """The frozen document of an issued version, the internal cost and margin withheld without ``pricing_internal``."""
    from engines.quotation_payload import project_issued_snapshot_for_actor

    document = version.document_payload
    if document is None:
        return None
    internal = can_see_internal(user)
    return {**document, "payload": plain(project_issued_payload_for_actor(document.get("payload"), internal)), "snapshot": plain(project_issued_snapshot_for_actor(document.get("snapshot"), internal))}


def engine_tier(tier: str) -> str:
    return ENGINE_TIER[tier]
