"""Customer location approval (PLAN §3.4 ``approval/request/`` and ``/api/customer/v1/inspection-approvals/<token>/``).

Request (staff with ``submit`` or ``approve``; COMPLETED, or CUSTOMER_APPROVAL_PENDING to issue a fresh link): the
site must be SUITABLE or CONDITIONAL; a PENDING approval is created with the **location snapshot** (both current
rectangles, ``engines.inspection_readiness.build_location_snapshot``), the customer's name and E.164 phone, and a
random 256-bit link token of which only the SHA-256 is stored (7-day expiry); an earlier PENDING approval is
superseded; the inspection moves to CUSTOMER_APPROVAL_PENDING — all in one transaction (V2 left orphan rows, §J 29).
The link is returned once to the requester, who sends it to the customer (SMS/WhatsApp); Twilio Verify only carries
the one-time code.

Customer side (no session, the token is the capability): ``GET`` shows the two annotated photos (10-minute signed
URLs), measurements and the proposed system — no internal remarks, no prices; ``send-otp/`` sends a code to the
approval's phone through the leads Twilio Verify client (purpose APPROVAL); ``respond/`` checks the code against that
phone, then records the decision, comment, optional drawn signature (private SIGNATURE asset) and client IP, and moves
the inspection to APPROVED or REJECTED (a rejection needs a comment). Only the latest, PENDING, unexpired approval of an
inspection that is waiting for it can be answered; staff can never answer for the customer.

Paper fallback (D-13, ``approve``): a Project Head records a paper-signed approval with the uploaded scan and a
reason; audited, ``approved_by_staff`` set, no OTP.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from core.services import stamp_create
from customers.services.phones import try_normalise
from leads.models import OtpRequest
from leads.services import otp
from media.models import MediaAsset
from media.services import assets
from site_inspections.models import Inspection, LocationApproval
from site_inspections.models.choices import ApprovalStatus, Status, Suitability
from site_inspections.services import common, photos

CUSTOMER = "CUSTOMER"  # audit actor kind of the customer answering through the link
APPROVAL_FOLDER = "site-inspections/approvals"


def ttl() -> timedelta:
    return timedelta(days=int(getattr(settings, "SITE_INSPECTIONS_APPROVAL_TTL_DAYS", 7)))


def link_for(token: str) -> str:
    base = getattr(settings, "SITE_INSPECTIONS_APPROVAL_LINK_BASE", "") or "/api/customer/v1/inspection-approvals/"
    return f"{base.rstrip('/')}/{token}/"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def approvals_queryset(inspection: Inspection):
    return LocationApproval.objects.filter(inspection=inspection).select_related("approved_by_staff", "signature_asset", "paper_scan_asset").order_by("-number")


def _latest(inspection: Inspection) -> LocationApproval | None:
    return LocationApproval.objects.filter(inspection=inspection).order_by("-number").first()


def customer_phones(customer) -> list[str]:
    """The numbers the customer record holds (Sales-maintained): the only ones the one-time code may go to by default."""
    alt = try_normalise(customer.alt_phone) if customer.alt_phone else None
    return [phone for phone in dict.fromkeys((customer.phone_e164, alt)) if phone]


def _approval_phone(inspection: Inspection, user, requested: str) -> str:
    """The phone that proves the customer. The code must never reach a number the requester chose: an engineer (who
    receives the link) may only use the customer's own numbers; a Project Head (``approve``, who may also record a
    paper approval) may use another number or the KSEB registered phone — audited with the request."""
    known = customer_phones(inspection.customer)
    approver = common.can_act(user, "approve")
    phone = requested or (known[0] if known else "")
    if not phone and approver:
        phone = try_normalise(inspection.registered_phone_e164) or ""
    if not phone:
        raise DomainError("phone_required", "The customer has no phone number for the one-time code.", errors={"customer_phone": ["Required."]})
    if phone not in known and not approver:
        raise PermissionDenied(
            "phone_not_customer",
            "The one-time code can only go to a phone number on the customer record; ask a Project Head to use another number.",
            errors={"customer_phone": ["Not a phone number of this customer."]},
        )
    return phone


@transaction.atomic
def request_approval(instance: Inspection, *, user, customer_name: str = "", customer_phone_e164: str = "", expected_version=None) -> tuple[LocationApproval, str]:
    if not (common.can_act(user, "submit") or common.can_act(user, "approve")):
        raise PermissionDenied("submit_or_approve_required", "Requesting the customer's approval needs the site-inspection submit or approve permission.")
    inspection = common.lock(instance, expected_version)
    if inspection.status not in (Status.COMPLETED, Status.CUSTOMER_APPROVAL_PENDING):
        raise Conflict("invalid_status", "The customer's approval is requested once the inspection is completed.", errors={"status": [inspection.status]})
    if inspection.site_suitability not in (Suitability.SUITABLE, Suitability.CONDITIONAL):
        raise Conflict(
            "site_not_suitable", "Only a suitable or conditionally suitable site is sent for the customer's approval.", errors={"site_suitability": [inspection.site_suitability or "unset"]}
        )
    customer = inspection.customer
    phone = _approval_phone(inspection, user, customer_phone_e164)
    superseded = []
    for pending in LocationApproval.objects.select_for_update().filter(inspection=inspection, status=ApprovalStatus.PENDING):
        pending.versioned_update(user, status=ApprovalStatus.SUPERSEDED)
        superseded.append(pending.number)
    token = secrets.token_urlsafe(32)
    approval = LocationApproval(
        inspection=inspection,
        number=common.next_number(LocationApproval.all_objects.filter(inspection=inspection)),
        status=ApprovalStatus.PENDING,
        location_snapshot=common.location_snapshot(inspection),
        customer_name=(customer_name or customer.name)[:255],
        customer_phone_e164=phone,
        token_hash=hash_token(token),
        expires_at=timezone.now() + ttl(),
    )
    stamp_create(approval, user)
    approval.save()
    if inspection.status != Status.CUSTOMER_APPROVAL_PENDING:
        common.set_status(inspection, user, Status.CUSTOMER_APPROVAL_PENDING)
    common.audit("approval_requested", inspection, user, after={"approval": approval.number, "phone": phone, "expires_at": approval.expires_at.isoformat(), "superseded": superseded})
    common.changed(inspection)
    return approval, token


# ── customer side ───────────────────────────────────────────────────────────────────────────────────────────────────


def resolve(token: str) -> LocationApproval:
    """The approval a link token belongs to (404 for anything unknown — never says which part was wrong)."""
    approval = None
    if token and len(token) <= 128:
        approval = (
            LocationApproval.objects.select_related("inspection", "inspection__customer", "inspection__quoted_panel", "inspection__quoted_inverter", "inspection__quoted_battery")
            .filter(token_hash=hash_token(token))
            .first()
        )
    if approval is None or approval.inspection.deleted_at is not None:
        raise NotFound("approval_not_found", "This approval link is not valid.")
    return approval


def _answerable(approval: LocationApproval) -> None:
    if approval.status != ApprovalStatus.PENDING:
        raise Conflict("approval_closed", "This approval request has already been answered or replaced.", errors={"status": [approval.status]})
    if approval.expires_at is None or approval.expires_at <= timezone.now():
        raise DomainError("link_expired", "This approval link has expired. Ask us for a new one.", status=410)
    latest = _latest(approval.inspection)
    if latest is None or latest.pk != approval.pk or approval.inspection.status != Status.CUSTOMER_APPROVAL_PENDING:
        raise Conflict("approval_closed", "This approval request is no longer open.")


def masked_phone(phone: str) -> str:
    return f"{phone[:3]}******{phone[-2:]}" if len(phone) > 6 else "******"


def _component(component) -> str | None:
    return None if component is None else getattr(component, "name", None) or str(component.uid)


def summary(approval: LocationApproval, *, version: str, absolute=None) -> dict:
    """What the customer sees: the frozen rectangles on their photos, measurements, the proposed system. No remarks."""
    from media.services import signing

    inspection = approval.inspection
    stored = approval.location_snapshot or {}
    photo_uids = [entry.get("photo") for entry in stored.values() if isinstance(entry, dict) and entry.get("photo")]
    by_uid = {str(p.uid): p for p in photos.photos_queryset(inspection).filter(uid__in=photo_uids)}
    locations = []
    for kind in ("PANEL_AREA", "EQUIPMENT_AREA"):
        entry = stored.get(kind)
        if not isinstance(entry, dict):
            continue
        photo = by_uid.get(str(entry.get("photo")))
        signed = signing.signed_url(photo.asset, version=version, absolute=absolute) if photo is not None else None
        locations.append(
            {
                "annotation_type": kind,
                "photo_url": signed.url if signed else None,
                "photo_expires_at": signed.expires_at if signed else None,
                "geometry": entry.get("geometry"),
                "width_m": entry.get("width_m"),
                "height_m": entry.get("height_m"),
                "area_m2": entry.get("area_m2"),
            }
        )
    return {
        "inspection_number": inspection.number,
        "approval_number": approval.number,
        "status": approval.status,
        "customer_name": approval.customer_name,
        "phone_masked": masked_phone(approval.customer_phone_e164),
        "expires_at": approval.expires_at,
        "visit_date": inspection.visit_date,
        "site": {"address": inspection.address, "pincode": inspection.pincode, "district": inspection.district},
        "system": {
            "system_type": inspection.system_type,
            "size_kw": inspection.quoted_size_kw,
            "phase": inspection.phase or None,
            "panel": _component(inspection.quoted_panel),
            "inverter": _component(inspection.quoted_inverter),
            "battery": _component(inspection.quoted_battery),
        },
        "locations": locations,
        "responded_at": approval.responded_at,
    }


def send_customer_otp(token: str, *, ip: str | None) -> dict:
    approval = resolve(token)
    _answerable(approval)
    sent = otp.send_code(phone_e164=approval.customer_phone_e164, purpose=OtpRequest.Purpose.APPROVAL, ip=ip)
    return {"status": "pending", "phone_masked": masked_phone(approval.customer_phone_e164), "expires_at": sent.expires_at}


def respond(token: str, *, code: str, decision: str, comment: str = "", signature=None, ip: str | None = None) -> LocationApproval:
    approval = resolve(token)
    _answerable(approval)
    if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        raise DomainError("validation_error", "Answer APPROVED or REJECTED.", errors={"decision": ["Invalid decision."]})
    if decision == ApprovalStatus.REJECTED and not (comment or "").strip():
        raise DomainError("comment_required", "Tell us what should change.", errors={"comment": ["Required when you reject the location."]})
    verification = otp.verify_code(phone_e164=approval.customer_phone_e164, code=code, purpose=OtpRequest.Purpose.APPROVAL, ip=ip)
    asset = None
    if signature is not None:
        asset = assets.upload(user=None, file=signature, visibility=MediaAsset.Visibility.PRIVATE, kind=MediaAsset.Kind.SIGNATURE, folder=APPROVAL_FOLDER, allow_reserved=True)
    try:
        return _record_response(approval, decision=decision, comment=comment, asset=asset, ip=ip, verified_at=verification.verified_at)
    except Exception:
        if asset is not None:
            assets.delete_asset(asset, user=None)
        raise


@transaction.atomic
def _record_response(approval: LocationApproval, *, decision: str, comment: str, asset, ip: str | None, verified_at) -> LocationApproval:
    inspection = common.lock(approval.inspection)
    approval = LocationApproval.objects.select_for_update().select_related("inspection").get(pk=approval.pk)
    approval.inspection = inspection
    _answerable(approval)
    now = timezone.now()
    approval.versioned_update(None, status=decision, otp_verified_at=verified_at, responded_at=now, responded_ip=ip, customer_comment=(comment or "").strip(), signature_asset=asset)
    status = Status.APPROVED if decision == ApprovalStatus.APPROVED else Status.REJECTED
    common.set_status(inspection, None, status, note="customer response")
    common.audit("approval_answered", inspection, None, actor_kind=CUSTOMER, after={"approval": approval.number, "decision": decision, "ip": ip, "signature": bool(asset)})
    common.changed(inspection)
    return approval


def record_paper_approval(instance: Inspection, *, user, scan, reason: str, expected_version=None) -> LocationApproval:
    """D-13: a paper-signed approval, recorded by a Project Head with the scan and a reason."""
    if not (reason or "").strip():
        raise DomainError("reason_required", "Say why the approval was signed on paper.", errors={"reason": ["Required."]})
    if instance.status != Status.CUSTOMER_APPROVAL_PENDING:
        raise Conflict("invalid_status", "A paper approval answers a pending customer approval.", errors={"status": [instance.status]})
    head = scan.read(5)
    scan.seek(0)
    kind = MediaAsset.Kind.DOCUMENT if head.startswith(b"%PDF") else MediaAsset.Kind.PHOTO
    asset = assets.upload(user=user, file=scan, visibility=MediaAsset.Visibility.PRIVATE, kind=kind, folder=APPROVAL_FOLDER, allow_reserved=True)
    try:
        return _record_paper(instance, user=user, asset=asset, reason=reason.strip(), expected_version=expected_version)
    except Exception:
        assets.delete_asset(asset, user=user)
        raise


@transaction.atomic
def _record_paper(instance: Inspection, *, user, asset, reason: str, expected_version) -> LocationApproval:
    inspection = common.lock(instance, expected_version)
    if inspection.status != Status.CUSTOMER_APPROVAL_PENDING:
        raise Conflict("invalid_status", "A paper approval answers a pending customer approval.", errors={"status": [inspection.status]})
    approval = LocationApproval.objects.select_for_update().filter(inspection=inspection, status=ApprovalStatus.PENDING).first()
    if approval is None:
        raise Conflict("approval_closed", "There is no pending approval to answer.")
    approval.versioned_update(user, status=ApprovalStatus.APPROVED, responded_at=timezone.now(), approved_by_staff=user, paper_scan_asset=asset, paper_reason=reason)
    common.set_status(inspection, user, Status.APPROVED, note="paper approval")
    common.audit("approval_recorded_on_paper", inspection, user, after={"approval": approval.number, "scan": str(asset.uid), "reason": reason})
    common.changed(inspection)
    return approval
