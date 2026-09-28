"""Helpers shared by the hr services: office-local dates, row locks, unique-violation mapping, cache namespaces."""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime
from functools import lru_cache

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from core.errors import Conflict, DomainError, NotFound
from core.services import check_version
from flarize.cache_utils import bump

# Version-keyed namespaces (standard §7.1) for readers that cache HR data (attendance, dashboards).
NS_SETUP = "hr:setup"  # offices, shifts, holidays, leave types, attendance rules
NS_EMPLOYEES = "hr:employees"
NS_LEAVE = "hr:leave"


def bump_setup() -> None:
    bump(NS_SETUP)


@lru_cache(maxsize=1)
def available_timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def validate_timezone(value: str) -> str:
    """An IANA zone name known to zoneinfo (PLAN §2.9 "validated against zoneinfo"), else 400."""
    value = (value or "").strip()
    if value not in available_timezones():
        raise DomainError("validation_error", "Invalid input.", errors={"timezone": [f"Unknown time zone {value!r}; use an IANA name such as Asia/Kolkata."]})
    return value


def today_in(tz_name: str | None = None) -> date:
    """Today in ``tz_name`` (an office's zone), else in the platform zone (A10: office tz for every "today")."""
    try:
        zone = zoneinfo.ZoneInfo(tz_name) if tz_name else zoneinfo.ZoneInfo(settings.TIME_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = zoneinfo.ZoneInfo(settings.TIME_ZONE)
    return timezone.now().astimezone(zone).date()


def now() -> datetime:
    return timezone.now()


def lock(model, instance, expected_version=None, *, manager: str = "objects", related: tuple[str, ...] = ()):
    """Re-read ``instance`` with ``SELECT … FOR UPDATE`` (own row only) and check the client's version."""
    queryset = getattr(model, manager).select_for_update(of=("self",))
    if related:
        queryset = queryset.select_related(*related)
    row = queryset.filter(pk=instance.pk).first()
    if row is None:
        raise NotFound("not_found", f"{model._meta.verbose_name.capitalize()} not found.")
    check_version(row, expected_version)
    return row


def unique_conflict(exc: IntegrityError, mapping: dict[str, tuple[str, str, str]]) -> Conflict | None:
    """Translate a partial-unique violation into a stable 409 (``mapping``: constraint → (code, field, message))."""
    text = str(exc)
    for constraint, (code, field, message) in mapping.items():
        if constraint in text:
            return Conflict(code, message, errors={field: ["Already in use."]})
    return None
