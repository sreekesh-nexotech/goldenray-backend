"""Helpers shared by the devices services: identity normalisation, cache namespaces, settings, row locks."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from core.errors import DomainError
from flarize.cache_utils import bump
from hr.services.common import lock, unique_conflict  # noqa: F401 - re-exported for the devices services (same HR context)

# Version-keyed namespaces (standard §7.1) for readers that cache device data (dashboards, office summaries).
NS_DEVICES = "devices:devices"
NS_AGENTS = "devices:agents"
NS_DEVICE_USERS = "devices:device_users"
NS_PROTOCOL = "devices:protocol"

_MAC_STRIP = re.compile(r"[^0-9a-f]")


def bump_devices(*extra: str) -> None:
    bump(NS_DEVICES, *extra)


def now() -> datetime:
    return timezone.now()


def setting(name: str, default):
    """``settings.<name>`` with a default (the devices knobs need no entry in the settings module)."""
    return getattr(settings, name, default)


def online_seconds() -> int:
    """A terminal heard from within this many seconds is ONLINE (PLAN: the ADMS thresholds, for every transport)."""
    return int(setting("DEVICES_ONLINE_SECONDS", 300))


def offline_seconds() -> int:
    """Quiet longer than :func:`online_seconds` but within this many seconds is DEGRADED; beyond it OFFLINE."""
    return int(setting("DEVICES_OFFLINE_SECONDS", 900))


# --------------------------------------------------------------------------------------------------------------------
# Identity values: the serial is identity, the MAC a second objection, the LAN address only a location.
# --------------------------------------------------------------------------------------------------------------------
def normalize_serial(value) -> str | None:
    """Trimmed serial, or ``None`` when none was given."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def mac_digits(value) -> str | None:
    """Twelve lower-case hex digits, or ``None`` when ``value`` is not a MAC (separators and case ignored)."""
    if value is None:
        return None
    digits = _MAC_STRIP.sub("", str(value).strip().lower())
    return digits if len(digits) == 12 else None


def normalize_mac(value) -> str:
    """``aa:bb:cc:dd:ee:ff`` or ``""`` (unusable values are never compared loosely)."""
    digits = mac_digits(value)
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)) if digits else ""


def macs_match(expected, reported) -> bool | None:
    """``True``/``False``, or ``None`` when either side is missing or unreadable ("cannot tell" never matches)."""
    a, b = mac_digits(expected), mac_digits(reported)
    if a is None or b is None:
        return None
    return a == b


def serials_match(expected, reported) -> bool | None:
    a, b = normalize_serial(expected), normalize_serial(reported)
    if a is None or b is None:
        return None
    return a == b


def normalize_ip(value) -> str | None:
    """A valid IPv4/IPv6 address as text, or ``None``."""
    if value in (None, ""):
        return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def normalize_networks(values, *, field: str = "allowed_ips") -> list[str]:
    """Canonical CIDR strings (``10.0.0.5`` → ``10.0.0.5/32``); 400 listing each invalid entry."""
    if values in (None, ""):
        return []
    if not isinstance(values, (list, tuple)):
        raise DomainError("validation_error", "Invalid input.", errors={field: ["Must be a list of IP addresses or CIDR networks."]})
    result, errors = [], []
    for value in values:
        try:
            network = str(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            errors.append(f"{value!r} is not an IP address or CIDR network.")
            continue
        if network not in result:
            result.append(network)
    if errors:
        raise DomainError("validation_error", "Invalid input.", errors={field: errors})
    return result


def ip_allowed(address: str | None, networks: list[str] | None) -> bool:
    """Whether ``address`` falls inside ``networks`` (an empty list allows every address)."""
    if not networks:
        return True
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for network in networks:
        try:
            if ip in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            continue
    return False
