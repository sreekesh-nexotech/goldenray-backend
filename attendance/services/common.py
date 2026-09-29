"""Helpers shared by the attendance services: clocks, office time zones, cache namespaces, date validation."""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

from core.errors import DomainError
from engines import attendance as engine
from flarize.cache_utils import bump

NS_DAYS = "attendance:days"  # processed days and corrections (reports, calendars, dashboards)
NS_PUNCHES = "attendance:punches"


def bump_days() -> None:
    bump(NS_DAYS)


def now() -> datetime:
    return timezone.now()


def platform_timezone() -> str:
    name = getattr(settings, "TIME_ZONE", engine.DEFAULT_TIMEZONE)
    return name if engine.is_valid_timezone(name) else engine.DEFAULT_TIMEZONE


def office_timezone(office) -> str:
    """The office's zone (validated on write by hr), else the platform zone — never the server's."""
    name = getattr(office, "timezone", None) if office is not None else None
    return name if name and engine.is_valid_timezone(name) else platform_timezone()


def zone_of(office) -> zoneinfo.ZoneInfo:
    return engine.zone(office_timezone(office))


def today_for(office=None, *, at: datetime | None = None) -> date:
    """Today on the office's wall clock (A10); the platform zone without an office."""
    return engine.today_local(at or now(), office_timezone(office))


def validate_range(date_from: date, date_to: date, *, max_days: int | None = None, field: str = "date_to") -> None:
    if date_to < date_from:
        raise DomainError("validation_error", "date_to is before date_from.", errors={field: ["Must not be before date_from."]})
    if max_days is not None and (date_to - date_from).days + 1 > max_days:
        raise DomainError("range_too_long", f"The range covers more than {max_days} days.", errors={field: [f"At most {max_days} days."]})


def latest_possible_today(at: datetime | None = None) -> date:
    """The latest "today" of any office (UTC+14 at most): no recompute needs a date beyond it (A7)."""
    return ((at or now()) + timedelta(hours=14)).date()
