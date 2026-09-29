"""What the pack engines need besides the configuration: the Flarize-shaped catalog priced by a PriceRelease, and the
pins of every pack of a version."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packs.models import ConfigPin, ConfigVersion
from packs.services.catalog_adapter import flarize_catalog, number
from pricing.models import PriceRelease
from pricing.services.releases import current_release


@dataclass
class EngineContext:
    catalog: dict
    price_release: PriceRelease | None

    @property
    def battery_master(self) -> dict:
        return self.catalog.get("batteryMaster") or {}


def release_prices(release: PriceRelease | None) -> dict[str, object]:
    """SKU → list price (a JavaScript number) from a PriceRelease payload; nothing without a release."""
    if release is None:
        return {}
    prices = {}
    for sku, item in (release.payload.get("components") or {}).items():
        if item.get("list_price") is not None:
            prices[sku] = number(Decimal(item["list_price"]))
    return prices


def engine_context(price_release: PriceRelease | None = None, *, use_current: bool = True) -> EngineContext:
    release = price_release if price_release is not None else (current_release() if use_current else None)
    return EngineContext(catalog=flarize_catalog(release_prices(release)), price_release=release)


def version_pins(version: ConfigVersion) -> dict[str, list[dict]]:
    """``{pack key: [{slot_key, sku, authoritative, alternates}]}`` of the version's live packs."""
    pins: dict[str, list[dict]] = {}
    rows = ConfigPin.objects.filter(pack__config_version=version, pack__deleted_at__isnull=True).select_related("pack", "component").order_by("pack_id", "id")
    for pin in rows:
        pins.setdefault(pin.pack.key, []).append({"slot_key": pin.slot_key, "sku": pin.component.sku, "authoritative": pin.authoritative, "alternates": list(pin.alternates or [])})
    return pins
