"""Input normalisation shared by the API services and the legacy importer (one rule, two callers)."""

from __future__ import annotations

import phonenumbers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email

from core.errors import DomainError

DEFAULT_PHONE_REGION = "IN"


def invalid(field: str, message: str) -> DomainError:
    return DomainError("validation_error", "Invalid input.", errors={field: [message]})


def code(value, field: str = "code", max_length: int = 50) -> str:
    value = (value or "").strip()
    if not value:
        raise invalid(field, "This field may not be blank.")
    if len(value) > max_length:
        raise invalid(field, f"Ensure this field has no more than {max_length} characters.")
    return value


def weekdays(value, field: str) -> list[int]:
    """A list of distinct weekdays 0=Monday … 6=Sunday, returned sorted."""
    if not isinstance(value, (list, tuple)):
        raise invalid(field, "Must be a list of weekdays (0=Monday … 6=Sunday).")
    days = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6:
            raise invalid(field, "Weekdays are whole numbers 0 (Monday) to 6 (Sunday).")
        if item in days:
            raise invalid(field, f"Weekday {item} is listed twice.")
        days.append(item)
    return sorted(days)


def phone_e164(value, field: str = "phone_e164", region: str = DEFAULT_PHONE_REGION) -> str:
    """E.164 form of a phone number (Indian numbers without a country code are accepted); blank stays blank."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        raise invalid(field, "Enter a valid phone number.") from None
    if not phonenumbers.is_valid_number(parsed):
        raise invalid(field, "Enter a valid phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def email(value, field: str = "email") -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        validate_email(value)
    except DjangoValidationError:
        raise invalid(field, "Enter a valid e-mail address.") from None
    return value
