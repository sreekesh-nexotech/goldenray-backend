"""Shared vocabulary of the packs services: engine ↔ platform names, pack keys, canonical JSON."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder

from core.errors import DomainError
from packs.models import SystemType, Tier
from pricing.services.common import parse_size_key

CACHE_NAMESPACE = "packs"
SYSTEM_LABELS = {SystemType.ONGRID: "On-grid", SystemType.HYBRID: "Hybrid"}


def engine_system(system_type: str) -> str:
    return system_type.lower()


def platform_system(system: str) -> str:
    return {"ongrid": SystemType.ONGRID, "hybrid": SystemType.HYBRID}[system]


def platform_tier(tier: str) -> str:
    return {"base": Tier.BASE, "value": Tier.VALUE, "premium": Tier.PREMIUM}[tier]


def size_kw(size_key: str) -> Decimal:
    parsed = parse_size_key(str(size_key))
    if parsed is None:
        raise DomainError("validation_error", f"{size_key!r} is not a size key.", errors={"size_key": ["A size key like 3, 5sp or 10."]})
    return parsed[0]


def pack_key(system: str, tier: str, size: str, battery: str = "", future: str = "") -> str:
    key = f"{system}-{tier}-{size}".lower()
    if battery != "":
        key += f"-b{battery}"
    if future:
        key += f"-up{future}".lower()
    return key


def validation_error(errors: dict, message: str = "Invalid input.") -> DomainError:
    return DomainError("validation_error", message, errors=errors)


def canonical_json(value) -> str:
    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_safe(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def sha256_of(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))
