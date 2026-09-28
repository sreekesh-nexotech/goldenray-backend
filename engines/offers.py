"""Offer lifecycle and application rules — port of Flarize ``src/lib/offerLifecycle.js``.

An offer is a standing campaign reduction from the pre-GST list selling price (a discount is per quotation).

    DRAFT → APPROVED → ACTIVE → EXPIRED → ARCHIVED        (APPROVED → DRAFT, EXPIRED → DRAFT, * → ARCHIVED)

Every transition records who and when; every content change bumps ``offerVersion`` (an APPROVED offer edited goes back
to DRAFT). The applicable offer is the newest ACTIVE one whose dates cover the day and whose system / tier / size
targets match (``'all'`` matches everything). The JS read the clock; here ``now`` (ISO timestamp) and ``date``
(ISO day) are parameters that default to the current UTC time.

Transition functions mutate the offer passed in (like the JS) and return it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from engines._jscompat import JsError, clean, is_nullish, js_min, js_number, js_or, js_str, js_trim, jsget, locale_compare, strict_equal, truthy

OFFER_TYPE = {"FLAT": "flat", "PERCENTAGE": "percentage"}
OFFER_STATUS = {"DRAFT": "DRAFT", "APPROVED": "APPROVED", "ACTIVE": "ACTIVE", "EXPIRED": "EXPIRED", "ARCHIVED": "ARCHIVED"}
VALID_TRANSITIONS = {
    "DRAFT": ("APPROVED", "ARCHIVED"),
    "APPROVED": ("ACTIVE", "DRAFT", "ARCHIVED"),
    "ACTIVE": ("EXPIRED", "ARCHIVED"),
    "EXPIRED": ("ARCHIVED", "DRAFT"),
    "ARCHIVED": (),
}
OFFER_ERROR = {name: name for name in ("INVALID_TYPE", "INVALID_VALUE", "MARGIN_BREACH", "OFFER_NOT_FOUND", "MISSING_FIELD", "INVALID_TRANSITION", "ALREADY_ARCHIVED", "NOT_DRAFT", "NOT_APPROVED")}
EDITABLE_FIELDS = ("name", "type", "value", "appliesTo", "appliesToTier", "appliesToSize", "startDate", "endDate", "description")


class OfferError(JsError):
    js_name = "OfferError"


def now_iso() -> str:
    """``new Date().toISOString()``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def validate_offer(offer: Any) -> dict:
    """``{valid, errors}`` for an offer definition (name, type, non-negative value, ≤ 100 %, start ≤ end)."""
    errors = []
    name = jsget(offer, "name")
    if not truthy(name) or not js_trim(js_str(name)):
        errors.append("Offer name is required")
    offer_type = jsget(offer, "type")
    if not truthy(offer_type) or offer_type not in OFFER_TYPE.values():
        errors.append(f"Offer type must be one of: {', '.join(OFFER_TYPE.values())}")
    value = jsget(offer, "value")
    number = js_number(value)
    if is_nullish(value) or number != number or number < 0:
        errors.append("Offer value must be a non-negative number")
    if offer_type == OFFER_TYPE["PERCENTAGE"] and number > 100:
        errors.append("Percentage offer cannot exceed 100%")
    start, end = jsget(offer, "startDate"), jsget(offer, "endDate")
    if truthy(start) and truthy(end) and js_str(start) > js_str(end):
        errors.append("startDate must be on or before endDate")
    return {"valid": not errors, "errors": errors}


def margin_safety_check(list_price: Any, cost: Any, offer: Any, min_margin: Any) -> dict:
    """Would the offer take the gross margin ``(net − cost) / net`` below ``min_margin`` (a fraction)?"""
    if not truthy(list_price) or not truthy(cost) or js_number(list_price) <= 0 or js_number(cost) <= 0:
        return {"safe": False, "effectiveMargin": 0, "breach": None, "reason": "Missing price or cost data"}
    price, cost, minimum = js_number(list_price), js_number(cost), js_number(min_margin)
    offer_amount: int | float = 0
    offer_type = jsget(offer, "type")
    if offer_type == OFFER_TYPE["FLAT"]:
        offer_amount = js_number(jsget(offer, "value"))
    elif offer_type == OFFER_TYPE["PERCENTAGE"]:
        offer_amount = price * (js_number(jsget(offer, "value")) / 100)
    net_price = price - offer_amount
    if net_price <= 0:
        return clean({"safe": False, "effectiveMargin": -1, "breach": min_margin, "reason": "Offer exceeds list price"})
    effective = (net_price - cost) / net_price
    return {
        "safe": effective >= minimum,
        "effectiveMargin": effective,
        "breach": minimum - effective if effective < minimum else None,
        "offerAmount": offer_amount,
        "netPrice": net_price,
    }


def is_valid_transition(from_status: Any, to_status: Any) -> bool:
    allowed = VALID_TRANSITIONS.get(from_status) if isinstance(from_status, str) else None
    return allowed is not None and to_status in allowed


def transition_offer(offer: dict, to_status: Any, changed_by: Any, reason: Any = None, *, now: str | None = None) -> dict:
    """Move an offer to ``to_status`` (``OfferError`` INVALID_TRANSITION otherwise); returns ``{offer, transition}``."""
    from_status = js_or(offer.get("status"), OFFER_STATUS["DRAFT"])
    if not is_valid_transition(from_status, to_status):
        allowed = VALID_TRANSITIONS.get(from_status, ()) if isinstance(from_status, str) else ()
        raise OfferError(
            f"Cannot transition from {js_str(from_status)} to {js_str(to_status)}",
            OFFER_ERROR["INVALID_TRANSITION"],
            {"from": from_status, "to": to_status, "allowed": list(allowed)},
        )
    at = now or now_iso()
    transition = {"from": from_status, "to": to_status, "changedBy": changed_by, "changedAt": at, "reason": reason if truthy(reason) else None}
    offer["status"] = to_status
    offer["changedBy"] = changed_by
    offer["changedAt"] = at
    if not truthy(offer.get("transitions")):
        offer["transitions"] = []
    offer["transitions"].append(transition)
    offer["active"] = to_status == OFFER_STATUS["ACTIVE"]
    return {"offer": offer, "transition": transition}


def create_draft_offer(fields: Any, created_by: Any, *, now: str | None = None) -> dict:
    """A new DRAFT offer with its creation transition."""
    fields = fields if isinstance(fields, dict) else {}
    at = now or now_iso()
    value = js_number(jsget(fields, "value"))
    return {
        "id": js_or(jsget(fields, "id"), f"offer_{_iso_to_ms(at)}"),
        "name": js_or(jsget(fields, "name"), "New Offer"),
        "type": js_or(jsget(fields, "type"), OFFER_TYPE["FLAT"]),
        "value": value if truthy(value) else 0,
        "appliesTo": js_or(jsget(fields, "appliesTo"), "all"),
        "appliesToTier": js_or(jsget(fields, "appliesToTier"), "all"),
        "appliesToSize": js_or(jsget(fields, "appliesToSize"), "all"),
        "startDate": js_or(jsget(fields, "startDate"), ""),
        "endDate": js_or(jsget(fields, "endDate"), ""),
        "description": js_or(jsget(fields, "description"), ""),
        "status": OFFER_STATUS["DRAFT"],
        "active": False,
        "offerVersion": 1,
        "createdAt": at,
        "createdBy": created_by,
        "changedAt": at,
        "changedBy": created_by,
        "transitions": [{"from": None, "to": OFFER_STATUS["DRAFT"], "changedBy": created_by, "changedAt": at, "reason": "Offer created"}],
    }


def update_offer(offer: dict, updates: Any, changed_by: Any, *, now: str | None = None) -> dict:
    """Edit a DRAFT or APPROVED offer (``offerVersion`` + 1; an APPROVED offer returns to DRAFT). ``{offer, reverted}``."""
    if offer.get("status") not in (OFFER_STATUS["DRAFT"], OFFER_STATUS["APPROVED"]):
        raise OfferError(
            f"Cannot edit offer in {js_str(jsget(offer, 'status'))} status. Only DRAFT and APPROVED offers can be edited.",
            OFFER_ERROR["INVALID_TRANSITION"],
            clean({"status": jsget(offer, "status")}),
        )
    at = now or now_iso()
    updates = updates if isinstance(updates, dict) else {}
    changed = False
    for field in EDITABLE_FIELDS:
        if field in updates and not strict_equal(updates[field], jsget(offer, field)):
            offer[field] = updates[field]
            changed = True
    reverted = False
    if changed:
        version = offer.get("offerVersion")
        offer["offerVersion"] = (version if truthy(version) else 1) + 1
        offer["changedAt"] = at
        offer["changedBy"] = changed_by
        if offer.get("status") == OFFER_STATUS["APPROVED"]:
            transition_offer(offer, OFFER_STATUS["DRAFT"], changed_by, "Content changed — reverted to DRAFT for re-approval", now=at)
            reverted = True
    return {"offer": offer, "reverted": reverted}


def auto_expire_offers(offers: Any, as_of: str | None = None, *, now: str | None = None) -> list:
    """Expire every ACTIVE offer whose ``endDate`` is before ``as_of`` (ISO day; default today); returns them."""
    if not isinstance(offers, list):
        return []
    at = now or now_iso()
    today = as_of if truthy(as_of) else at[:10]
    expired = []
    for offer in offers:
        end = offer.get("endDate")
        if offer.get("status") == OFFER_STATUS["ACTIVE"] and truthy(end) and js_str(end) < today:
            transition_offer(offer, OFFER_STATUS["EXPIRED"], "SYSTEM", "Auto-expired: endDate passed", now=at)
            expired.append(offer)
    return expired


def find_applicable_offer(offers: Any, *, system_type: Any = None, tier: Any = None, size: Any = None, date: str | None = None) -> dict | None:
    """The most recently created ACTIVE offer valid on ``date`` whose targets match (``'all'`` matches everything)."""
    if not truthy(offers) or not isinstance(offers, list):
        return None
    today = date if truthy(date) else now_iso()[:10]

    def applies(offer: dict) -> bool:
        if offer.get("status") != OFFER_STATUS["ACTIVE"]:
            return False
        start, end = offer.get("startDate"), offer.get("endDate")
        if truthy(start) and js_str(start) > today:
            return False
        if truthy(end) and js_str(end) < today:
            return False
        for key, wanted in (("appliesTo", system_type), ("appliesToTier", tier), ("appliesToSize", size)):
            target = offer.get(key)
            if truthy(target) and target != "all" and not strict_equal(target, wanted):
                return False
        return True

    active = [o for o in offers if applies(o)]
    if not active:
        return None
    best = active[0]
    for offer in active[1:]:
        if locale_compare(js_str(js_or(offer.get("createdAt"), "")), js_str(js_or(best.get("createdAt"), ""))) > 0:
            best = offer
    return best


def calculate_offer_amount(offer: Any, list_price: Any) -> int | float:
    """FLAT → ``min(value, listPrice)``; PERCENTAGE → ``listPrice × value / 100``; ``listPrice`` is pre-GST."""
    if not truthy(offer) or not truthy(list_price) or js_number(list_price) <= 0:
        return 0
    offer_type = jsget(offer, "type")
    if offer_type == OFFER_TYPE["FLAT"]:
        return js_min(js_number(jsget(offer, "value")), list_price)
    if offer_type == OFFER_TYPE["PERCENTAGE"]:
        return js_number(list_price) * (js_number(jsget(offer, "value")) / 100)
    return 0


def offer_payload_shape(offer: Any, list_price: Any) -> dict:
    """The quotation payload's offer section (no arithmetic beyond :func:`calculate_offer_amount`)."""
    if not truthy(offer):
        return {"available": False, "source": "NO_ACTIVE_OFFER", "value": None}
    return {
        "available": True,
        "source": "OFFER_MASTER",
        "value": {
            "offerId": js_or(jsget(offer, "id"), None),
            "offerName": js_or(jsget(offer, "name"), None),
            "offerType": jsget(offer, "type"),
            "offerValue": js_number(jsget(offer, "value")),
            "offerAmount": calculate_offer_amount(offer, list_price),
            "offerVersion": js_or(jsget(offer, "offerVersion"), 1),
            "offerStatus": jsget(offer, "status"),
            "appliesTo": js_or(jsget(offer, "appliesTo"), "all"),
            "appliesToTier": js_or(jsget(offer, "appliesToTier"), "all"),
            "startDate": js_or(jsget(offer, "startDate"), None),
            "endDate": js_or(jsget(offer, "endDate"), None),
        },
    }
