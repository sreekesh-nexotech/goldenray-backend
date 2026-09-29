"""Hardware identity, compared the way the platform compares it (``devices.services.common``).

The serial number the terminal reports is the identity. The MAC is a second, independent signal that can raise an
objection but never stands in for a serial. The LAN address is not identity at all: 192.168.1.209 exists in every
office, so an address only says where to knock, never who answered. A missing value compares as "cannot tell"
(``None``), never as "matches".
"""

from __future__ import annotations

import re

_MAC_STRIP = re.compile(r"[^0-9a-f]")


def normalize_serial(value: object) -> str | None:
    """Trimmed serial, or ``None`` when the terminal did not give one."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_mac(value: object) -> str | None:
    """Twelve lowercase hex digits (separators and case ignored), or ``None`` when it is not a MAC."""
    if value is None:
        return None
    digits = _MAC_STRIP.sub("", str(value).strip().lower())
    return digits if len(digits) == 12 else None


def display_mac(value: object) -> str | None:
    """``aa:bb:cc:dd:ee:ff`` (the platform's stored form), or ``None``."""
    digits = normalize_mac(value)
    return ":".join(digits[index : index + 2] for index in range(0, 12, 2)) if digits else None


def serials_match(expected: object, reported: object) -> bool | None:
    a, b = normalize_serial(expected), normalize_serial(reported)
    if a is None or b is None:
        return None
    return a == b


def macs_match(expected: object, reported: object) -> bool | None:
    a, b = normalize_mac(expected), normalize_mac(reported)
    if a is None or b is None:
        return None
    return a == b
