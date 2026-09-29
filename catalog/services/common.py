"""Helpers shared by the catalog services: cache namespace, JSON-safe values, media asset rules, slugs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.text import slugify

from core.errors import DomainError
from media.models import MediaAsset

CACHE_NAMESPACE = "catalog"
# Public product payloads embed prices (pricing wave) and media URLs.
PUBLIC_CACHE_NAMESPACES = (CACHE_NAMESPACE, "pricing", "media")


def json_safe(value):
    """``value`` as plain JSON types (Decimal → string, dates → ISO), for audit/change-log snapshots."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def validation_error(errors: dict, message: str = "Invalid input.") -> DomainError:
    return DomainError("validation_error", message, errors=errors)


@dataclass(frozen=True)
class AssetRule:
    kinds: frozenset[str]
    public: bool = True


IMAGE_RULE = AssetRule(frozenset({MediaAsset.Kind.IMAGE, MediaAsset.Kind.PHOTO}))
DOCUMENT_RULE = AssetRule(frozenset({MediaAsset.Kind.DOCUMENT}))


def asset_problem(asset: MediaAsset | None, rule: AssetRule) -> str | None:
    """Why ``asset`` cannot be used under ``rule`` (``None`` when it can)."""
    if asset is None:
        return None
    if asset.deleted_at is not None:
        return "The file has been deleted."
    if asset.kind not in rule.kinds:
        return f"Must be a {' or '.join(sorted(rule.kinds))} file."
    if rule.public and not asset.is_public:
        return "Must be a public file (it is shown on the website)."
    return None


def check_assets(values: dict, rules: dict[str, AssetRule]) -> None:
    errors = {}
    for field, rule in rules.items():
        if field in values:
            problem = asset_problem(values[field], rule)
            if problem:
                errors[field] = [problem]
    if errors:
        raise validation_error(errors)


def unique_slug(base: str, taken, *, max_length: int = 120, fallback: str = "item") -> str:
    """``base`` slugified, suffixed ``-2``, ``-3`` … until ``taken(slug)`` is false."""
    root = (slugify(base) or fallback)[:max_length].strip("-") or fallback
    candidate, counter = root, 2
    while taken(candidate):
        suffix = f"-{counter}"
        candidate = f"{root[: max_length - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
