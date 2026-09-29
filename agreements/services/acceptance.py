"""Customer acceptance on paper and price overrides.

* ``record_acceptance`` (PLAN D-13 "paper-signed customer approval fallback — allowed, audited") — an ISSUED agreement
  becomes ACCEPTED (``accepted_via = PAPER``) with the scan of the signed copy stored as a **private** media asset in
  the reserved folder ``agreements/acceptance`` (never listed by the media library). The bytes are stored before the
  transaction; a refused write discards them.
* ``override_price`` (Plan 2 D2-1, PLAN D-1 flag ``AGREEMENTS_PRICE_OVERRIDE``) — the only way a pinned price moves:
  flag on, ``agreements.manage``, a reason, a DRAFT; ``discount`` or ``final_price`` is set and the other follows
  (final = original + extra − discount); the change and the reason are audited.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.services.authz import can
from agreements.models import AcceptedVia, Agreement, AgreementStatus
from agreements.services.common import ACCEPTANCE_FOLDER, CACHE_NAMESPACE, MODULE, lock, money, require_draft
from audit.services import record
from core.errors import Conflict, DomainError, NotFound, PermissionDenied
from core.flags import flag_enabled
from core.outbox import emit
from flarize.cache_utils import bump

logger = logging.getLogger("flarize.agreements")
FLAG = "AGREEMENTS_PRICE_OVERRIDE"
PDF_MAGIC = b"%PDF"


def _require_issued(agreement: Agreement) -> None:
    if agreement.status != AgreementStatus.ISSUED:
        raise Conflict("agreement_not_issued", f"Only an ISSUED agreement can be accepted (this one is {agreement.status}).", errors={"status": [agreement.status]})


def _store_scan(file, user):
    from media.models import MediaAsset
    from media.services.assets import upload

    head = file.read(4)
    file.seek(0)
    kind = MediaAsset.Kind.DOCUMENT if head == PDF_MAGIC else MediaAsset.Kind.PHOTO
    try:
        return upload(user=user, file=file, visibility=MediaAsset.Visibility.PRIVATE, kind=kind, folder=ACCEPTANCE_FOLDER, allow_reserved=True)
    except DomainError as exc:
        messages = [message for values in (exc.errors or {}).values() for message in values] or [exc.message]
        raise DomainError(exc.code, exc.message, status=exc.status, errors={"file": messages}) from None


def _discard(asset) -> None:
    from media.services.assets import delete_asset

    try:
        delete_asset(asset, user=None)
    except Exception:  # noqa: BLE001 - best effort; the asset stays private and unreferenced
        logger.warning("could not discard an acceptance scan", extra={"asset": str(asset.uid)}, exc_info=True)


def record_acceptance(instance: Agreement, *, user, file, note: str = "", expected_version=None) -> Agreement:
    """Store the scan first (no row locks held during storage), then record the acceptance in one transaction."""
    current = Agreement.objects.filter(pk=instance.pk).first()
    if current is None:
        raise NotFound("not_found", "Agreement not found.")
    _require_issued(current)
    asset = _store_scan(file, user)
    try:
        return _accept(instance, user=user, asset=asset, note=note, expected_version=expected_version)
    except BaseException:
        _discard(asset)
        raise


@transaction.atomic
def _accept(instance: Agreement, *, user, asset, note: str, expected_version) -> Agreement:
    agreement = lock(instance, expected_version)
    _require_issued(agreement)
    now = timezone.now()
    agreement.versioned_update(
        user,
        status=AgreementStatus.ACCEPTED,
        accepted_at=now,
        accepted_via=AcceptedVia.PAPER,
        acceptance_asset=asset,
        acceptance_note=(note or "").strip(),
        acceptance_recorded_by=user if getattr(user, "pk", None) else None,
    )
    record("agreements.accepted", obj=agreement, actor=user, after={"status": agreement.status, "accepted_via": agreement.accepted_via, "asset": str(asset.uid), "note": agreement.acceptance_note})
    emit(
        "agreements.accepted",
        {
            "agreement_uid": str(agreement.uid),
            "customer_uid": str(agreement.customer.uid),
            "kind": agreement.kind,
            "number": agreement.number,
            "accepted_at": now.isoformat(),
            "accepted_via": agreement.accepted_via,
        },
        aggregate_type="agreements.agreement",
        aggregate_uid=agreement.uid,
    )
    bump(CACHE_NAMESPACE)
    return agreement


@transaction.atomic
def override_price(instance: Agreement, *, user, reason: str, discount=None, final_price=None, expected_version=None) -> Agreement:
    if not flag_enabled(FLAG):
        raise Conflict("price_override_disabled", "Price overrides are switched off (AGREEMENTS_PRICE_OVERRIDE).")
    if not can(user, MODULE, "manage"):
        raise PermissionDenied()
    agreement = lock(instance, expected_version)
    require_draft(agreement)
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["Required."]})
    if (discount is None) == (final_price is None):
        raise DomainError("validation_error", "Send either discount or final_price.", errors={"discount": ["Send exactly one of discount and final_price."]})
    if agreement.original_price is None:
        raise Conflict("price_missing", "The agreement has no original price to override.")
    total = agreement.original_price + (agreement.extra_cost or Decimal(0))
    if discount is not None:
        new_discount = money(discount)
        new_final = money(total - new_discount) if new_discount is not None else None
    else:
        new_final = money(final_price)
        new_discount = money(total - new_final) if new_final is not None else None
    if new_discount is None or new_final is None or new_discount < 0 or new_final < 0:
        raise DomainError("validation_error", "The price must stay between 0 and the original price plus extras.", errors={"discount" if discount is not None else "final_price": ["Out of range."]})
    before = {"discount": str(agreement.discount), "final_price": str(agreement.final_price), "price_override_reason": agreement.price_override_reason}
    agreement.versioned_update(user, discount=new_discount, final_price=new_final, price_override_reason=reason)
    record("agreements.price_overridden", obj=agreement, actor=user, before=before, after={"discount": str(new_discount), "final_price": str(new_final), "price_override_reason": reason})
    bump(CACHE_NAMESPACE)
    return agreement
