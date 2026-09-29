"""Old website forms: ``/api/lead-collection-home/``, ``/api/send-otp/``, ``/api/verify-otp/``,
``/api/affiliate-applications/``, ``/api/warranty-service-requests/`` and ``/api/job-applications/``.

Each form is validated by the platform's canonical serializer with the legacy field names renamed
(``phone_number`` → ``phone``, ``source`` → ``form``; docs/decisions/leads-customers.md) and stored by the owning
service (``leads.services.intake``, ``leads.services.otp``, ``careers.services.applications``). Answers are the legacy
bodies (``{"message": "Validation failed", "status": "error", "errors": {…legacy field names…}}`` and the legacy
success shapes, phone numbers as 10 national digits, labels for choice values, legacy/shim integer ids).

* ``lead-collection-home``: no OTP (DV-61: the legacy form had none).
* ``verify-otp``: an approved code also records the enquiry, as the legacy ``record_lead`` did (form QUOTE_REQUEST,
  page ``/advanced-calculator``). The legacy ``SentQuote`` row (``quote_id``) belongs to the quotations package: the
  shim answers the legacy success body with ``quote_id`` = ``QUOTE_<8 hex of the lead uid>`` until quotations is
  integrated (listed in docs/decisions/legacy-shim.md).
"""

from __future__ import annotations

from django.http import QueryDict
from rest_framework import serializers

from careers.models import JobPosition
from careers.serializers.public import PublicJobApplicationSerializer
from careers.services import applications
from core.errors import DomainError
from customers.services.phones import InvalidPhone, national_digits, normalise_phone
from leads.models import Lead, OtpRequest
from leads.serializers.public import AffiliateSubmitSerializer, LeadSubmitSerializer, WarrantySubmitSerializer
from leads.services import intake, otp
from legacy.services.ids import BACKEND, CMS, legacy_id, resolve_pk

_DATETIME = serializers.DateTimeField()
LEAD_FIELDS = {"phone_number": "phone", "source": "form"}
LEAD_MESSAGE = "Thank you! We'll be in touch shortly."
AFFILIATE_MESSAGE = "Message sent!"
WARRANTY_MESSAGE = "Service request received. Our team will contact you shortly."
APPLICATION_MESSAGE = "Application received. Our team will get in touch if there's a fit."


class LegacyValidationFailed(DomainError):
    """400 with the legacy body ``{"message": "Validation failed", "status": "error", "errors": {…}}``."""

    def __init__(self, errors: dict):
        super().__init__("validation_error", "Validation failed", status=400, errors=errors)


class LegacyPlainErrors(DomainError):
    """400 with the bare DRF ``serializer.errors`` dict (the legacy OTP views)."""

    def __init__(self, errors: dict):
        super().__init__("validation_error", "Validation failed", status=400, errors=errors)


def _renamed_errors(detail, back: dict[str, str]) -> dict:
    detail = detail if isinstance(detail, dict) else {"non_field_errors": detail}
    return {back.get(key, key): [str(message) for message in (value if isinstance(value, list) else [value])] for key, value in detail.items()}


def _validated(serializer_class, data, *, back: dict[str, str] | None = None):
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        raise LegacyValidationFailed(_renamed_errors(serializer.errors, back or {}))
    return serializer


def _stamp(value) -> str:
    return _DATETIME.to_representation(value)


# ── lead-collection-home ─────────────────────────────────────────────────────────────────────────────────────────
def _lead_data(body) -> dict:
    body = body if hasattr(body, "items") else {}
    return {LEAD_FIELDS.get(key, key): value for key, value in body.items() if key in ("name", "phone_number", "source", "page", "details", "website")}


def lead_body(lead: Lead) -> dict:
    return {
        "id": legacy_id(Lead, lead.pk, system=BACKEND, table="lead_collection_home"),
        "name": lead.name,
        "phone_number": national_digits(lead.phone_e164),
        "source": lead.form.lower(),
        "source_label": lead.get_form_display(),
        "page": lead.source_url,
        "details": (lead.payload or {}).get("details", {}),
        "created_at": _stamp(lead.created_at),
        "updated_at": _stamp(lead.updated_at),
    }


def _required(body, fields) -> dict:
    """The legacy ModelSerializer's messages for its required fields (the canonical form words them differently)."""
    errors = {}
    for field in fields:
        if field not in body:
            errors[field] = ["This field is required."]
        elif not str(body[field] or "").strip():
            errors[field] = ["This field may not be blank."]
    return errors


def submit_lead(body, *, ip: str | None) -> dict:
    back = {value: key for key, value in LEAD_FIELDS.items()}
    body = body if hasattr(body, "items") else {}
    required = _required(body, ("name", "phone_number"))
    try:
        serializer = _validated(LeadSubmitSerializer, _lead_data(body), back=back)
    except LegacyValidationFailed as exc:
        raise LegacyValidationFailed({**exc.errors, **required}) from None
    if required:  # the legacy form required a phone even where the canonical one accepts an e-mail instead
        raise LegacyValidationFailed(required)
    data = dict(serializer.validated_data)
    data.pop("verification_token", None)
    try:
        lead = intake.submit_lead(data=data, ip=ip, require_verification=False)
    except DomainError as exc:
        raise LegacyValidationFailed(_renamed_errors(exc.errors or {"non_field_errors": [exc.message]}, back)) from None
    return {**lead_body(lead), "message": LEAD_MESSAGE, "status": "success"}


# ── OTP ──────────────────────────────────────────────────────────────────────────────────────────────────────────
class _SendOtp(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)
    name = serializers.CharField(max_length=100, required=False)


class _VerifyOtp(_SendOtp):
    code = serializers.CharField(max_length=10)


def _otp_phone(serializer_class, body):
    serializer = serializer_class(data=body if hasattr(body, "items") else {})
    if not serializer.is_valid():
        raise LegacyPlainErrors(_renamed_errors(serializer.errors, {}))
    try:
        phone = normalise_phone(serializer.validated_data["phone_number"], mobile_only=True)
    except InvalidPhone:
        raise DomainError("otp_send_failed", "Enter a valid 10-digit Indian mobile number.") from None
    return serializer.validated_data, phone


def send_otp(body, *, ip: str | None) -> dict:
    _, phone = _otp_phone(_SendOtp, body)
    otp.send_code(phone_e164=phone, purpose=OtpRequest.Purpose.LEAD, ip=ip)
    return {"status": "pending"}


def verify_otp(body, *, ip: str | None) -> dict:
    data, phone = _otp_phone(_VerifyOtp, body)
    verification = otp.verify_code(phone_e164=phone, code=data["code"], ip=ip)
    lead_data = {"name": data.get("name", ""), "phone_e164": phone, "form": Lead.Form.QUOTE_REQUEST, "source_url": "/advanced-calculator", "payload": {}}
    lead = intake.submit_lead(data=lead_data, ip=ip, verification_token=verification.token)
    return {"status": "approved", "message": "OTP verified successfully. Your quote request has been recorded.", "quote_id": f"QUOTE_{lead.uid.hex[:8].upper()}"}


# ── affiliate / warranty ─────────────────────────────────────────────────────────────────────────────────────────
def submit_affiliate(body) -> dict:
    serializer = _validated(AffiliateSubmitSerializer, body if hasattr(body, "items") else {})
    application = intake.submit_affiliate_application(data=serializer.validated_data)
    data = {
        "id": legacy_id(type(application), application.pk, system=BACKEND, table="affiliate_application"),
        "full_name": application.full_name,
        "phone": national_digits(application.phone_e164),
        "email": application.email,
        "profession": application.get_profession_display(),
        "district": application.get_district_display(),
        "created_at": _stamp(application.created_at),
    }
    return {"message": AFFILIATE_MESSAGE, "status": "success", "data": data}


def submit_warranty(body) -> dict:
    serializer = _validated(WarrantySubmitSerializer, body if hasattr(body, "items") else {})
    request = intake.submit_warranty_request(data=serializer.validated_data)
    data = {
        "id": legacy_id(type(request), request.pk, system=BACKEND, table="warranty_service_request"),
        "full_name": request.full_name,
        "phone": national_digits(request.phone_e164),
        "issue_type": request.get_issue_type_display(),
        "description": request.description,
        "created_at": _stamp(request.created_at),
    }
    return {"message": WARRANTY_MESSAGE, "status": "success", "data": data}


# ── job applications ─────────────────────────────────────────────────────────────────────────────────────────────
def _position_uid(raw) -> tuple[str | None, str | None]:
    """The legacy integer ``position_id`` → the posting's uid (``(uid, error)``)."""
    if raw in (None, ""):
        return None, None
    try:
        value = int(str(raw))
    except ValueError:
        return None, "A valid integer is required."
    pk = resolve_pk(JobPosition, value, system=CMS, table="careers_job_position")
    uid = JobPosition.all_objects.filter(pk=pk).values_list("uid", flat=True).first() if pk else None
    return (str(uid), None) if uid else (None, f'Invalid pk "{value}" - object does not exist.')


def submit_application(data, *, ip: str | None) -> dict:
    """``data`` is the multipart ``request.data`` (a QueryDict with the files): kept a QueryDict so the form keeps
    DRF's HTML-input rules (an empty optional field is its default, a missing checkbox is false), as in the legacy."""
    fields = QueryDict(mutable=True)
    for key, values in data.lists():  # a shallow copy: QueryDict.copy() deep-copies (and cannot copy) uploaded files
        fields.setlist(key, values)
    uid, error = _position_uid(fields.pop("position_id", [None])[0] if "position_id" in fields else None)
    if error:
        raise LegacyValidationFailed({"position_id": [error]})
    if uid:
        fields["position_id"] = uid
    serializer = _validated(PublicJobApplicationSerializer, fields)
    values, resume, portfolio = serializer.to_service()
    application = applications.submit_application(data=values, resume=resume, portfolio=portfolio, ip=ip)
    return {"message": APPLICATION_MESSAGE, "status": "success", "data": application_body(application)}


def application_body(application) -> dict:
    position = application.position
    return {
        "id": legacy_id(type(application), application.pk, system=BACKEND, table="job_application"),
        "position": application.position_label,
        "position_id": legacy_id(JobPosition, position.pk, system=CMS, table="careers_job_position") if position else None,
        "position_title": application.position_title,
        "department_name": application.department_name,
        "display_position": application.display_position,
        "status": application.status.lower(),
        "status_changed_at": _stamp(application.status_changed_at) if application.status_changed_at else None,
        "archived_at": None,
        "full_name": application.name,
        "email": application.email,
        "phone": national_digits(application.phone_e164),
        "location": application.location,
        "linkedin": application.linkedin,
        "portfolio_website": application.portfolio_website,
        "current_company": application.current_company,
        "current_role": application.current_role,
        "total_experience": application.total_experience,
        "relevant_experience": application.relevant_experience,
        "current_salary": application.current_salary,
        "expected_salary": application.expected_salary,
        "notice_period": application.notice_period,
        "heard_about_us": application.heard_about_us,
        "availability": application.availability,
        "cover_note": application.cover_letter,
        "resume": None,
        "portfolio_file": None,
        "resume_download_url": None,
        "portfolio_download_url": None,
        "declaration_accepted": application.declaration_accepted,
        "created_at": _stamp(application.created_at),
    }
