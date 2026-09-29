"""The website's view of the current PackRelease (PLAN §3.3 ``packs``, ``packs/{system_type}/{tier}/{size_kw}``).

Customer prices and a BOM summary (category, name, quantity); never landed cost, margins, component unit prices or the
pack-pricing ``internal`` block. ``size_kw`` in the path is a size key (``3``, ``5sp``, ``5tp``) or a number of kW
(``5`` matches both 5 kW packs).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from core.errors import DomainError, NotFound
from packs.models import ReleasePack, SystemType, Tier
from packs.services.releases import current_release
from pricing.services.common import SIZE_KEY_RE


def public_queryset():
    release = current_release()
    if release is None:
        return ReleasePack.objects.none()
    return ReleasePack.objects.filter(release=release).select_related("release").order_by("sort_order")


def bom_summary(bom: list) -> list[dict]:
    return [{"category": line.get("category"), "name": line.get("name"), "qty": line.get("qty")} for line in bom or [] if line.get("source") != "STRUCTURE"]


def _choice(value: str, choices, field: str) -> str:
    upper = (value or "").upper()
    if upper not in choices.values:
        raise DomainError("validation_error", f"Unknown {field} {value!r}.", errors={field: [f"One of {', '.join(choices.values)} (case-insensitive)."]})
    return upper


def size_filter(value: str) -> dict:
    text = (value or "").strip().lower()
    if not SIZE_KEY_RE.match(text):
        raise DomainError("validation_error", f"{value!r} is not a size.", errors={"size_kw": ["A size in kW (3, 5, 10) or a size key (5sp, 5tp)."]})
    if text.endswith(("sp", "tp")):
        return {"size_key": text}
    try:
        return {"size_kw": Decimal(text)}
    except InvalidOperation:  # pragma: no cover - the pattern admits only decimal numbers
        raise DomainError("validation_error", "Invalid size.", errors={"size_kw": ["Invalid size."]}) from None


def packs_for(system_type: str, tier: str, size: str):
    system = _choice(system_type, SystemType, "system_type")
    tier_value = _choice(tier, Tier, "tier")
    rows = list(public_queryset().filter(system_type=system, tier=tier_value, **size_filter(size)))
    if not rows:
        raise NotFound("pack_not_found", "No published pack matches.")
    return system, tier_value, rows
