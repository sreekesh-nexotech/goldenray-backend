"""Helpers shared by the pricing services: cache namespaces, the ``pricing_internal`` gate, value helpers, hashing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from core.errors import DomainError

# Public product pages (catalog) are cached under "pricing": bumped on every PriceRelease.
CACHE_NAMESPACE = "pricing"
# Staff reads of the authoring tables are not cached; this namespace exists for consumers that cache derived data.
AUTHORING_NAMESPACE = "pricing:authoring"

SIZE_KEY_RE = re.compile(r"^(?P<kw>\d+(?:\.\d+)?)(?P<phase>sp|tp)?$")
PHASE_OF_SUFFIX = {"sp": "1P", "tp": "3P"}
SUFFIX_OF_PHASE = {"1P": "sp", "3P": "tp"}


def can_see_internal(user) -> bool:
    """``pricing_internal.view``: unlocks landed cost, purchase-to-landed build-up and margin fields in responses."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from accounts.services.authz import can

    return can(user, "pricing_internal", "view")


def validation_error(errors: dict, message: str = "Invalid input.") -> DomainError:
    return DomainError("validation_error", message, errors=errors)


def json_safe(value):
    """``value`` as plain JSON types (Decimal → string, dates → ISO) for audit snapshots and payloads."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def canonical_json(value) -> str:
    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def today() -> date:
    return timezone.localdate()


def money_text(amount: Decimal | None) -> str | None:
    """A money amount as the fixed-point string payloads carry (``"13585.00"``)."""
    return None if amount is None else format(Decimal(amount).quantize(Decimal("0.01")), "f")


def decimal_text(value: Decimal | None) -> str | None:
    """Any decimal as its canonical plain string (no exponent, trailing zeros removed: ``"5"``, ``"0.1"``)."""
    if value is None:
        return None
    normalised = Decimal(value).normalize()
    text = format(normalised, "f")
    return text


def parse_size_key(key: str) -> tuple[Decimal, str] | None:
    """``"5sp"`` → ``(Decimal("5"), "1P")``; ``"10"`` → ``(Decimal("10"), "")``; ``None`` when not a size key."""
    match = SIZE_KEY_RE.match(str(key or ""))
    if not match:
        return None
    return Decimal(match.group("kw")), PHASE_OF_SUFFIX.get(match.group("phase") or "", "")


MAX_SIZE_KW = Decimal("9999.99")  # numeric(6,2), the size columns (size_kw, from_size_kw, applies_to_size_kw)


def valid_size_key(key: str) -> tuple[Decimal, str] | None:
    """:func:`parse_size_key` limited to the sizes the ``numeric(6,2)`` columns hold: more than 0, at most
    ``9999.99`` kW, at most 2 decimal places (``None`` otherwise, so the caller reports a validation error)."""
    parsed = parse_size_key(key)
    if parsed is None:
        return None
    size_kw = parsed[0]
    if size_kw <= 0 or size_kw > MAX_SIZE_KW or size_kw != size_kw.quantize(Decimal("0.01")):
        return None
    return parsed


def size_key_for(size_kw: Decimal, phase: str = "") -> str:
    return f"{decimal_text(size_kw)}{SUFFIX_OF_PHASE.get(phase, '')}"
