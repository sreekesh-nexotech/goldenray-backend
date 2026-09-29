"""Shared helpers of the quotations services: vocabulary between the platform and the engines, money, cache names."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.utils import timezone

from accounts.services.authz import can
from engines.frozen import jsonable

CACHE_NAMESPACE = "quotations"
PUBLIC_TESTIMONIALS_NAMESPACE = "quotations_testimonials"
CONTENT_NAMESPACE = "quotation_content"

MODULE = "quotations"
CONTENT_MODULE = "quotation_content"

CENT = Decimal("0.01")
FOUR = Decimal("0.0001")

#: platform enum → the Flarize/engine vocabulary (and back).
ENGINE_SYSTEM = {"ONGRID": "ongrid", "HYBRID": "hybrid"}
PLATFORM_SYSTEM = {value: key for key, value in ENGINE_SYSTEM.items()}
ENGINE_TIER = {"BASE": "base", "VALUE": "value", "PREMIUM": "premium"}
PLATFORM_TIER = {value: key for key, value in ENGINE_TIER.items()}
TIER_ORDER = ("base", "value", "premium")


def money(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fraction(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(FOUR, rounding=ROUND_HALF_UP)


def js_number(value: Any) -> Any:
    """A Decimal/int as the JavaScript number an engine expects (int when whole, else float)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def size_kw_of(size_key: str) -> Decimal:
    """``5sp`` → 5, ``3`` → 3 (the phase suffix stripped, as Flarize's P0-1 fix)."""
    digits = "".join(ch for ch in str(size_key) if ch.isdigit() or ch == ".")
    return Decimal(digits or "0")


def phase_for(size_key: str, three_phase_sizes: list | tuple = ()) -> str:
    """Flarize: ``5tp`` → 3P, ``5sp`` → 1P, a template three-phase size → 3P, else 1P."""
    if str(size_key).endswith("tp") or str(size_key) in {str(size) for size in three_phase_sizes or ()}:
        return "3P"
    return "1P"


def iso(value: dt.datetime | None = None) -> str:
    """ISO-8601 in UTC with milliseconds and ``Z`` (the Flarize timestamp form the engines parse)."""
    value = value or timezone.now()
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.astimezone(dt.UTC).microsecond // 1000:03d}Z"


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def can_see_internal(user) -> bool:
    """``pricing_internal.view`` unlocks landed cost, reference cost and margin (PLAN §3.2)."""
    return can(user, "pricing_internal", "view")


def plain(value: Any) -> Any:
    """Plain JSON data of an engine document (frozen containers thawed, Decimals as numbers)."""
    return jsonable(value)
