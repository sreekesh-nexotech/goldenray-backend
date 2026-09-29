"""``pricing/offers/`` — campaign offers and their lifecycle (PLAN §2.3, §3.4; DV-19).

The rules are ``engines.offers`` (the golden-tested port of Flarize ``offerLifecycle.js``): validation
(:func:`engines.offers.validate_offer`), the transition table (:data:`engines.offers.VALID_TRANSITIONS`), the edit rule
(only DRAFT/APPROVED offers change; a content change bumps ``content_version`` and sends an APPROVED offer back to
DRAFT), auto-expiry and the applicable-offer choice. The platform adds PLAN's PAUSED: ACTIVE → PAUSED → ACTIVE, and a
PAUSED offer can still expire or be archived.

Endpoints: ``approve/`` (DRAFT → APPROVED, ``offers.approve``), ``activate/`` (APPROVED/PAUSED → ACTIVE,
``offers.publish``), ``pause/`` (ACTIVE → PAUSED, ``offers.publish``), ``archive/`` (any → ARCHIVED,
``offers.archive``). ``pricing.tasks.expire_offers`` (Beat, daily) expires offers whose ``ends_on`` has passed.
Every step is a ``pricing_offer_transition`` row, an audit row and a ``pricing.offer_status_changed`` event.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.models import actor_or_none
from core.outbox import emit
from core.sequences import next_number
from core.services import check_version, stamp_create
from engines import offers as offer_engine
from pricing.models import Offer, OfferStatus, OfferSystem, OfferTier, OfferTransition, OfferType
from pricing.services.common import decimal_text, today, valid_size_key

EDITABLE_FIELDS = ("name", "type", "value", "applies_to_system", "applies_to_tier", "applies_to_size_key", "starts_on", "ends_on", "print_on_quotation", "stackable", "description")
SNAPSHOT_FIELDS = ("code", *EDITABLE_FIELDS, "status", "content_version")
CONTENT_FIELDS = ("name", "type", "value", "applies_to_system", "applies_to_tier", "applies_to_size_key", "starts_on", "ends_on", "description")
PLATFORM_TRANSITIONS = {
    **{status: tuple(targets) for status, targets in offer_engine.VALID_TRANSITIONS.items()},
    OfferStatus.ACTIVE: (*offer_engine.VALID_TRANSITIONS["ACTIVE"], OfferStatus.PAUSED),
    OfferStatus.PAUSED: (OfferStatus.ACTIVE, OfferStatus.EXPIRED, OfferStatus.ARCHIVED),
}
AUDIT_ACTIONS = {
    OfferStatus.DRAFT: "pricing.offer_reopened",
    OfferStatus.APPROVED: "pricing.offer_approved",
    OfferStatus.ACTIVE: "pricing.offer_activated",
    OfferStatus.PAUSED: "pricing.offer_paused",
    OfferStatus.EXPIRED: "pricing.offer_expired",
    OfferStatus.ARCHIVED: "pricing.offer_archived",
}
TYPE_TO_ENGINE = {OfferType.FLAT: offer_engine.OFFER_TYPE["FLAT"], OfferType.PERCENT: offer_engine.OFFER_TYPE["PERCENTAGE"]}
TYPE_FROM_ENGINE = {engine: platform for platform, engine in TYPE_TO_ENGINE.items()}


def offers_queryset():
    return Offer.objects.prefetch_related(Prefetch("transitions", queryset=OfferTransition.objects.select_related("by").order_by("at", "id"))).order_by("-created_at", "-id")


def as_engine_offer(offer: Offer) -> dict:
    """The Flarize offer shape ``engines.offers`` works on (values as JS numbers, dates as ISO days or '')."""
    return {
        "id": offer.code,
        "name": offer.name,
        "type": TYPE_TO_ENGINE[offer.type],
        "value": float(offer.value) if offer.value != offer.value.to_integral_value() else int(offer.value),
        "appliesTo": "all" if offer.applies_to_system == OfferSystem.ALL else offer.applies_to_system.lower(),
        "appliesToTier": "all" if offer.applies_to_tier == OfferTier.ALL else offer.applies_to_tier.lower(),
        "appliesToSize": offer.applies_to_size_key or "all",
        "startDate": offer.starts_on.isoformat() if offer.starts_on else "",
        "endDate": offer.ends_on.isoformat() if offer.ends_on else "",
        "description": offer.description,
        "status": offer.status,
        "active": offer.status == OfferStatus.ACTIVE,
        "offerVersion": offer.content_version,
        "createdAt": offer.created_at.isoformat().replace("+00:00", "Z") if offer.created_at else "",
    }


def _validate(values: dict) -> None:
    probe = {
        "name": values.get("name"),
        "type": TYPE_TO_ENGINE.get(values.get("type")),
        "value": float(values["value"]) if values.get("value") is not None else None,
        "startDate": values["starts_on"].isoformat() if values.get("starts_on") else "",
        "endDate": values["ends_on"].isoformat() if values.get("ends_on") else "",
    }
    result = offer_engine.validate_offer(probe)
    if not result["valid"]:
        field_of = {"name": "name", "type": "type", "value": "value", "Percentage": "value", "startDate": "ends_on"}
        errors: dict[str, list[str]] = {}
        for message in result["errors"]:
            field = next((column for word, column in field_of.items() if word in message), "non_field_errors")
            errors.setdefault(field, []).append(message)
        raise DomainError("validation_error", "Invalid offer.", errors=errors)
    size_key = values.get("applies_to_size_key") or ""
    if size_key and valid_size_key(size_key) is None:
        raise DomainError("validation_error", "Invalid size key.", errors={"applies_to_size_key": ["A size key like 3, 5sp, 5tp or 10 — more than 0 and at most 9999.99 kW (blank = every size)."]})


def _size_kw(size_key: str) -> Decimal | None:
    parsed = valid_size_key(size_key) if size_key else None
    return parsed[0] if parsed else None


def add_transition(offer: Offer, from_status: str, to_status: str, *, user, reason: str = "", at=None, by_label: str = "") -> OfferTransition:
    return OfferTransition.objects.create(offer=offer, from_status=from_status or "", to_status=to_status, at=at or timezone.now(), by=actor_or_none(user), by_label=by_label, reason=reason or "")


def _code_conflict() -> Conflict:
    return Conflict("offer_code_taken", "Another offer already uses this code.", errors={"code": ["Already in use."]})


@transaction.atomic
def create_offer(*, user, data: dict) -> Offer:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    _validate(values)
    code = (data.get("code") or "").strip() or next_number("OFFER", fmt="OFFER-{n}")
    offer = Offer(code=code, status=OfferStatus.DRAFT, status_changed_at=timezone.now(), applies_to_size_kw=_size_kw(values.get("applies_to_size_key", "")), **values)
    stamp_create(offer, user)
    try:
        with transaction.atomic():
            offer.save()
    except IntegrityError:
        raise _code_conflict() from None
    add_transition(offer, "", OfferStatus.DRAFT, user=user, reason="Offer created")
    record("pricing.offer_created", obj=offer, actor=user, after=snapshot(offer, SNAPSHOT_FIELDS))
    return offer


@transaction.atomic
def update_offer(instance: Offer, *, user, data: dict, expected_version=None) -> Offer:
    offer = Offer.objects.select_for_update().get(pk=instance.pk)
    check_version(offer, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and getattr(offer, name) != data[name]}
    if not values:
        return offer
    if offer.status not in (OfferStatus.DRAFT, OfferStatus.APPROVED):
        raise Conflict("offer_not_editable", f"Only DRAFT and APPROVED offers can be edited; this one is {offer.status}.", errors={"status": [offer.status]})
    merged = {name: values.get(name, getattr(offer, name)) for name in EDITABLE_FIELDS}
    _validate(merged)
    before = snapshot(offer, SNAPSHOT_FIELDS)
    if "applies_to_size_key" in values:
        values["applies_to_size_kw"] = _size_kw(values["applies_to_size_key"])
    content_changed = any(name in values for name in CONTENT_FIELDS)
    reverted = False
    if content_changed:
        values["content_version"] = offer.content_version + 1
        if offer.status == OfferStatus.APPROVED:
            values["status"] = OfferStatus.DRAFT
            values["status_changed_at"] = timezone.now()
            reverted = True
    offer.versioned_update(user, **values)
    if reverted:
        add_transition(offer, OfferStatus.APPROVED, OfferStatus.DRAFT, user=user, reason="Content changed — reverted to DRAFT for re-approval")
        _emit(offer, OfferStatus.APPROVED, OfferStatus.DRAFT)
    changed_before, changed_after = changes(before, snapshot(offer, SNAPSHOT_FIELDS))
    record("pricing.offer_updated", obj=offer, actor=user, before=changed_before, after=changed_after)
    return offer


def _emit(offer: Offer, from_status: str, to_status: str) -> None:
    emit(
        "pricing.offer_status_changed",
        {"offer_uid": str(offer.uid), "code": offer.code, "from": from_status, "to": to_status},
        aggregate_type="pricing.offer",
        aggregate_uid=offer.uid,
    )


def is_valid_transition(from_status: str, to_status: str) -> bool:
    return to_status in PLATFORM_TRANSITIONS.get(from_status, ())


def _transition(offer: Offer, to_status: str, *, user, reason: str = "", by_label: str = "") -> Offer:
    from_status = offer.status
    if not is_valid_transition(from_status, to_status):
        allowed = ", ".join(PLATFORM_TRANSITIONS.get(from_status, ())) or "none"
        raise Conflict("invalid_transition", f"Cannot move an offer from {from_status} to {to_status} (allowed: {allowed}).", errors={"status": [from_status]})
    offer.versioned_update(user, status=to_status, status_changed_at=timezone.now())
    add_transition(offer, from_status, to_status, user=user, reason=reason, by_label=by_label)
    record(AUDIT_ACTIONS[to_status], obj=offer, actor=user, before={"status": from_status}, after={"status": to_status}, note=reason)
    _emit(offer, from_status, to_status)
    return offer


def _locked(instance: Offer, expected_version) -> Offer:
    offer = Offer.objects.select_for_update().get(pk=instance.pk)
    check_version(offer, expected_version)
    return offer


@transaction.atomic
def approve(instance: Offer, *, user, reason: str = "", expected_version=None) -> Offer:
    offer = _locked(instance, expected_version)
    _validate({name: getattr(offer, name) for name in EDITABLE_FIELDS})
    return _transition(offer, OfferStatus.APPROVED, user=user, reason=reason or "Approved")


@transaction.atomic
def activate(instance: Offer, *, user, reason: str = "", expected_version=None) -> Offer:
    offer = _locked(instance, expected_version)
    if offer.ends_on is not None and offer.ends_on < today():
        raise Conflict("offer_already_ended", f"The offer ended on {offer.ends_on}; change its dates first.", errors={"ends_on": [str(offer.ends_on)]})
    return _transition(offer, OfferStatus.ACTIVE, user=user, reason=reason or ("Resumed" if offer.status == OfferStatus.PAUSED else "Activated"))


@transaction.atomic
def pause(instance: Offer, *, user, reason: str = "", expected_version=None) -> Offer:
    return _transition(_locked(instance, expected_version), OfferStatus.PAUSED, user=user, reason=reason or "Paused")


@transaction.atomic
def archive(instance: Offer, *, user, reason: str = "", expected_version=None) -> Offer:
    offer = _locked(instance, expected_version)
    if offer.status == OfferStatus.ARCHIVED:
        raise Conflict("offer_already_archived", "The offer is already archived.")
    return _transition(offer, OfferStatus.ARCHIVED, user=user, reason=reason or "Archived")


@transaction.atomic
def delete_offer(instance: Offer, *, user, expected_version=None) -> None:
    offer = _locked(instance, expected_version)
    if offer.status != OfferStatus.DRAFT:
        raise Conflict("offer_not_draft", "Only DRAFT offers are deleted; archive this one instead.", errors={"status": [offer.status]})
    offer.soft_delete(user)
    record("pricing.offer_deleted", obj=offer, actor=user, before=snapshot(offer, SNAPSHOT_FIELDS))


@transaction.atomic
def expire_due_offers(as_of: date | None = None) -> list[str]:
    """``auto_expire_offers``: ACTIVE and PAUSED offers whose ``ends_on`` is before ``as_of`` become EXPIRED."""
    as_of = as_of or today()
    expired = []
    for offer in Offer.objects.select_for_update(skip_locked=True).filter(status__in=[OfferStatus.ACTIVE, OfferStatus.PAUSED], ends_on__lt=as_of).order_by("id"):
        _transition(offer, OfferStatus.EXPIRED, user=None, reason="Auto-expired: endDate passed", by_label="SYSTEM")
        expired.append(offer.code)
    return expired


def applicable_offer(*, system_type: str, tier: str, size_key: str, on: date | None = None) -> Offer | None:
    """The offer ``engines.offers.find_applicable_offer`` chooses among ACTIVE offers (newest wins)."""
    offers = list(Offer.objects.filter(status=OfferStatus.ACTIVE))
    chosen = offer_engine.find_applicable_offer([as_engine_offer(offer) for offer in offers], system_type=system_type.lower(), tier=tier.lower(), size=size_key, date=(on or today()).isoformat())
    if chosen is None:
        return None
    return next(offer for offer in offers if offer.code == chosen["id"])


def offer_amount(offer: Offer, list_price: Decimal) -> Decimal:
    """``calculate_offer_amount`` on a pre-GST list price (FLAT → min(value, price); PERCENT → price × value %)."""
    amount = offer_engine.calculate_offer_amount(as_engine_offer(offer), float(list_price))
    return Decimal(repr(amount)).quantize(Decimal("0.01"))


def offer_summary(offer: Offer) -> dict:
    return {
        "code": offer.code,
        "name": offer.name,
        "type": offer.type,
        "value": decimal_text(offer.value),
        "applies_to_system": offer.applies_to_system,
        "applies_to_tier": offer.applies_to_tier,
        "applies_to_size_key": offer.applies_to_size_key,
        "starts_on": offer.starts_on.isoformat() if offer.starts_on else None,
        "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
        "status": offer.status,
    }
