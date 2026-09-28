"""Website submissions (PLAN §3.3): ``POST leads``, ``affiliate-applications``, ``warranty-requests``.

* A lead that carries a phone number needs the verification token from ``otp/verify`` for exactly that number
  (:func:`leads.services.otp.read_token`); ``otp_verified_at`` records when it was verified. Only the legacy
  adapters (``/legacy/api/lead-collection-home/``, which never had OTP) may pass ``require_verification=False``; such
  leads keep ``otp_verified_at`` empty so Studio can tell them apart.
* Every submission is kept, a repeat from the same number included (legacy behaviour: each one is a fresh enquiry).
  A lead or warranty request from the number of a live customer is linked to that customer (matched by phone only);
  a lead of a customer with an owner is assigned to that owner.
* ``kind`` and ``form`` complete each other: either may be omitted (the other decides); ``OTHER`` goes with any kind.
* ``payload`` is a validated document: ``details`` (the legacy flat ``details`` object: ≤ 30 scalar fields, text
  values cut at 2,000 characters, empty values dropped), ``calculator`` (inputs/outputs, ≤ 8 KB) and ``utm``.
"""

from __future__ import annotations

import json

from django.db import transaction

from audit.services import record, snapshot
from core.errors import DomainError
from core.sequences import next_number
from customers.services.customers import find_by_phone
from flarize.cache_utils import bump
from leads.models import AffiliateApplication, Lead, OtpRequest, WarrantyRequest
from leads.services import otp
from leads.services.leads import CACHE_NAMESPACE, created

PUBLIC = "CUSTOMER"
MAX_DETAIL_FIELDS = 30
MAX_DETAIL_VALUE = 2000
MAX_CALCULATOR_BYTES = 8 * 1024
MAX_SYSTEM_DETAILS_BYTES = 4 * 1024
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "referrer")

FORM_KIND = {
    Lead.Form.FOOTER: Lead.Kind.HOME_ENQUIRY,
    Lead.Form.HOME_BOOKING: Lead.Kind.HOME_ENQUIRY,
    Lead.Form.CONTACT_PAGE: Lead.Kind.CONTACT,
    Lead.Form.GROUP_PURCHASE: Lead.Kind.GROUP_PURCHASE,
    Lead.Form.QUOTATION: Lead.Kind.QUOTE_REQUEST,
    Lead.Form.QUOTE_REQUEST: Lead.Kind.ADVANCED_CALC,
    Lead.Form.REFERRAL_PARTNER: Lead.Kind.REFERRAL,
    Lead.Form.WARRANTY_SERVICE: Lead.Kind.CONTACT,
    Lead.Form.OTHER: Lead.Kind.CONTACT,
}
KIND_FORM = {
    Lead.Kind.HOME_ENQUIRY: Lead.Form.HOME_BOOKING,
    Lead.Kind.ADVANCED_CALC: Lead.Form.QUOTE_REQUEST,
    Lead.Kind.GROUP_PURCHASE: Lead.Form.GROUP_PURCHASE,
    Lead.Kind.CONTACT: Lead.Form.CONTACT_PAGE,
    Lead.Kind.REFERRAL: Lead.Form.REFERRAL_PARTNER,
    Lead.Kind.QUOTE_REQUEST: Lead.Form.QUOTATION,
}
# Kinds whose forms must carry a phone number (all but a contact message, which may leave only an e-mail address).
PHONE_REQUIRED_KINDS = frozenset(set(Lead.Kind.values) - {Lead.Kind.CONTACT})


class PayloadError(ValueError):
    pass


def resolve_kind_and_form(kind: str | None, form: str | None) -> tuple[str, str]:
    if not kind and not form:
        return Lead.Kind.CONTACT, Lead.Form.OTHER
    if not kind:
        return FORM_KIND[form], form
    if not form:
        return kind, KIND_FORM[kind]
    if form not in (Lead.Form.OTHER, Lead.Form.WARRANTY_SERVICE) and FORM_KIND[form] != kind:
        raise DomainError("validation_error", "This form does not collect that kind of lead.", errors={"form": [f"{form} goes with kind {FORM_KIND[form]}."]})
    return kind, form


def clean_details(value) -> dict:
    """The legacy ``details`` rule: a flat object of at most 30 scalars; empty values dropped, text cut at 2,000."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or len(value) > MAX_DETAIL_FIELDS:
        raise PayloadError("Invalid details.")
    clean = {}
    for key, item in value.items():
        if item in (None, ""):
            continue
        if not isinstance(item, (str, int, float, bool)):
            raise PayloadError("Invalid details.")
        clean[str(key)[:64]] = item[:MAX_DETAIL_VALUE] if isinstance(item, str) else item
    return clean


def _depth(value, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([_depth(item, level + 1) for item in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(item, level + 1) for item in value] or [level + 1])
    return level


def clean_document(value, *, max_bytes: int, max_depth: int = 4) -> dict:
    """A small JSON object (calculator inputs/outputs, system details): size and depth bounded."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise PayloadError("Must be a JSON object.")
    if len(json.dumps(value, ensure_ascii=False).encode()) > max_bytes:
        raise PayloadError(f"Must be at most {max_bytes // 1024} KB.")
    if _depth(value) > max_depth:
        raise PayloadError(f"Must be nested at most {max_depth} levels deep.")
    return value


def clean_utm(value) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or any(key not in UTM_KEYS for key in value):
        raise PayloadError(f"Allowed keys: {', '.join(UTM_KEYS)}.")
    return {key: str(item)[:500] for key, item in value.items() if item not in (None, "")}


def build_payload(*, details=None, calculator=None, utm=None) -> dict:
    payload = {"details": clean_details(details), "calculator": clean_document(calculator, max_bytes=MAX_CALCULATOR_BYTES), "utm": clean_utm(utm)}
    return {key: value for key, value in payload.items() if value}


@transaction.atomic
def submit_lead(*, data: dict, ip: str | None = None, verification_token: str | None = None, require_verification: bool = True) -> Lead:
    """``data``: validated ``kind``, ``form``, ``name``, ``phone_e164``, ``email``, ``pincode``, ``district``, ``message``,
    ``source_url``, ``payload``."""
    kind, form = resolve_kind_and_form(data.get("kind"), data.get("form"))
    phone = data.get("phone_e164", "")
    if not phone and kind in PHONE_REQUIRED_KINDS:
        raise DomainError("validation_error", "A phone number is required.", errors={"phone": ["This field is required."]})
    if not phone and not data.get("email"):
        raise DomainError("validation_error", "Give a phone number or an e-mail address.", errors={"phone": ["Give a phone number or an e-mail address."]})
    verified_at = None
    if phone and require_verification:
        verified_at = otp.read_token(verification_token or "", phone_e164=phone, purpose=OtpRequest.Purpose.LEAD)
    customer = find_by_phone(phone)
    lead = Lead(
        number=next_number("LEAD"),
        kind=kind,
        form=form,
        name=data["name"],
        phone_e164=phone,
        email=data.get("email", ""),
        pincode=data.get("pincode", ""),
        district=data.get("district", ""),
        message=data.get("message", ""),
        payload=data.get("payload") or {},
        source_url=data.get("source_url", ""),
        otp_verified_at=verified_at,
        ip=ip,
        customer=customer,
        assignee=customer.owner if customer is not None else None,
    )
    lead.save()
    created(lead, user=None, actor_kind=PUBLIC, channel="website")
    return lead


@transaction.atomic
def submit_affiliate_application(*, data: dict) -> AffiliateApplication:
    application = AffiliateApplication.objects.create(full_name=data["full_name"], phone_e164=data["phone_e164"], email=data["email"], profession=data["profession"], district=data["district"])
    record("leads.affiliate_application_received", obj=application, actor_kind=PUBLIC, after=snapshot(application, ("full_name", "phone_e164", "email", "profession", "district")))
    bump(CACHE_NAMESPACE)
    return application


@transaction.atomic
def submit_warranty_request(*, data: dict) -> WarrantyRequest:
    request = WarrantyRequest.objects.create(
        full_name=data["full_name"],
        phone_e164=data["phone_e164"],
        issue_type=data["issue_type"],
        description=data.get("description", ""),
        system_details=data.get("system_details") or {},
        customer=find_by_phone(data["phone_e164"]),
    )
    record("leads.warranty_request_received", obj=request, actor_kind=PUBLIC, after=snapshot(request, ("full_name", "phone_e164", "issue_type", "description", "customer")))
    bump(CACHE_NAMESPACE)
    return request
