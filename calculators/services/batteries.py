"""Backup batteries offered by the advanced calculator's hybrid option (DV-78).

The legacy view picked from the ``batteries`` table (capacity, price). That table is now the website's battery
products in the catalog (``catalog.services.legacy_import`` imports each row as a public battery component with a
PUBLISHED profile, ``battery_spec.capacity_kwh``, and returns its price for the pricing package), so the calculator
offers exactly what the website lists under ``products/batteries/``: published battery products with a capacity
and a current price from the price provider (``catalog.services.pricing_hooks``, filled by pricing). A battery
without a price is not offered — a quote is never built on a missing price.
"""

from __future__ import annotations

from catalog.services import pricing_hooks
from catalog.services.public import products_in
from engines.website_calculators import Battery

CACHE_NAMESPACES = ("catalog", "pricing")


def price_of(info: pricing_hooks.PriceInfo | None):
    if info is None:
        return None
    return info.min_amount if info.min_amount is not None else info.max_amount


def backup_batteries() -> tuple[Battery, ...]:
    """Published battery products with a capacity and a price, as engine rows (``position`` = component order)."""
    profiles = [profile for profile in products_in("battery") if getattr(profile.component, "battery_spec", None) is not None and profile.component.battery_spec.capacity_kwh is not None]
    prices = pricing_hooks.prices_for([profile.component for profile in profiles])
    batteries = []
    for profile in profiles:
        price = price_of(prices.get(profile.component.pk))
        if price is not None:
            batteries.append(Battery(capacity=profile.component.battery_spec.capacity_kwh, price=price, position=profile.component.pk))
    return tuple(batteries)
