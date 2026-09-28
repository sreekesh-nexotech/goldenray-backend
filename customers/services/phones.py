"""Phone numbers → E.164 with ``phonenumbers`` (default region IN). The one normaliser of the sales context.

Accepted spellings (a superset of what the legacy website forms accepted): ``9876543210``, ``09876543210``,
``+91 98765-43210``, ``919876543210``, ``0091 9876543210``, spaces/dashes/dots/parentheses anywhere. Website forms
and OTP ask for ``mobile_only`` Indian numbers (as the legacy ``^[6-9][0-9]{9}$`` rule did; it also keeps SMS off
premium and foreign destinations); staff entry accepts any valid number (a landline, a relative abroad).
"""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

DEFAULT_REGION = "IN"
INDIA = frozenset({"IN"})
_SEPARATORS = re.compile(r"[\s\-().]")
_MOBILE_TYPES = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})


class InvalidPhone(ValueError):
    """The value is not a phone number the caller accepts (the message is safe to show)."""


def normalise_phone(value, *, mobile_only: bool = False, regions: frozenset[str] | None = None) -> str:
    """``value`` as E.164 (``+919876543210``) or :class:`InvalidPhone`."""
    raw = str(value or "").strip()
    if not raw:
        raise InvalidPhone("Enter a phone number.")
    if len(raw) > 32:
        raise InvalidPhone("Enter a valid phone number.")
    compact = _SEPARATORS.sub("", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    try:
        number = phonenumbers.parse(compact, DEFAULT_REGION)
    except NumberParseException:
        raise InvalidPhone("Enter a valid phone number.") from None
    if not phonenumbers.is_valid_number(number):
        raise InvalidPhone("Enter a valid 10-digit Indian mobile number." if mobile_only else "Enter a valid phone number.")
    if regions is not None and phonenumbers.region_code_for_number(number) not in regions:
        raise InvalidPhone("Only Indian numbers are accepted.")
    if mobile_only and phonenumbers.number_type(number) not in _MOBILE_TYPES:
        raise InvalidPhone("Enter a valid 10-digit Indian mobile number.")
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def try_normalise(value, **kwargs) -> str | None:
    """:func:`normalise_phone` or ``None`` (importers report instead of failing)."""
    try:
        return normalise_phone(value, **kwargs)
    except InvalidPhone:
        return None


def national_digits(e164: str) -> str:
    """``+919876543210`` → ``9876543210`` (the legacy responses' 10-digit spelling)."""
    if not e164:
        return ""
    try:
        return str(phonenumbers.parse(e164, DEFAULT_REGION).national_number)
    except NumberParseException:
        return e164
