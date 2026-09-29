"""Phone numbers → E.164 with ``phonenumbers`` (default region IN). The one normaliser of the sales context.

Accepted spellings (a superset of the legacy website rule's spellings): ``9876543210``, ``09876543210``,
``+91 98765-43210``, ``919876543210``, ``0091 9876543210``, spaces/dashes/dots/parentheses anywhere, and the legacy
rule's own reading of a stray ``+`` (``+9876543210``, ``98765+43210``) when the text is no valid number otherwise.
Website forms and OTP ask for ``mobile_only`` Indian numbers (the legacy ``^[6-9][0-9]{9}$`` rule's intent; it also
keeps SMS off premium and foreign destinations) — stricter than the legacy regex in one way: a ten-digit number that the
numbering plan (``phonenumbers`` metadata) does not list as a mobile, e.g. a landline written without its trunk ``0``,
is refused. Staff entry accepts any valid number (a landline, a relative abroad).
"""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

DEFAULT_REGION = "IN"
INDIA = frozenset({"IN"})
_SEPARATORS = re.compile(r"[\s\-().]")
_MOBILE_TYPES = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})


_LEGACY_SEPARATORS = re.compile(r"[\s\-()+]")
_LEGACY_MOBILE = re.compile(r"^[6-9][0-9]{9}$")


class InvalidPhone(ValueError):
    """The value is not a phone number the caller accepts (the message is safe to show)."""


def _parse(compact: str):
    try:
        return phonenumbers.parse(compact, DEFAULT_REGION)
    except NumberParseException:
        return None


def _legacy_indian_mobile(raw: str) -> str | None:
    """The 10 digits the legacy ``validate_phone`` rule accepted (``[\\s\\-()+]`` dropped, a leading 91 cut), else ``None``."""
    digits = _LEGACY_SEPARATORS.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if _LEGACY_MOBILE.match(digits) else None


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
    number = _parse(compact)
    if number is None or not phonenumbers.is_valid_number(number):
        # The legacy website rule dropped every "+" and read 10 digits (or 91 + 10 digits) as an Indian mobile number:
        # "+9876543210" and "98765+43210" were accepted. Read a spelling that is no valid number that way too.
        legacy = _legacy_indian_mobile(raw)
        number = _parse(f"+91{legacy}") if legacy else number
    if number is None:
        raise InvalidPhone("Enter a valid phone number.")
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
