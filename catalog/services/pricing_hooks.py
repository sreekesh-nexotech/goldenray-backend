"""Price provider registry for the public product pages (PLAN §3.3: "price range from the current PriceRelease").

Catalog sits below pricing and never imports it. The pricing package registers one provider (from its
``AppConfig.ready()``)::

    from catalog.services import pricing_hooks

    @pricing_hooks.register
    def current_release_prices(components):
        return {component.pk: pricing_hooks.PriceInfo(min_amount=…, max_amount=…, release_number=…) for component in components}

A provider is called **once per response** with every component on the page (no N+1) and returns
``{component.pk: PriceInfo | None}``. The default provider knows no prices (returns ``{}``); the public payload then
falls back to the profile's ``price_range_label``. A failing provider is logged and treated as "no price" so the
website never breaks. Pricing bumps the ``pricing`` cache namespace on every release; the public product endpoints
are cached under ``catalog`` + ``pricing`` (+ ``media``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger("flarize.catalog.pricing")


@dataclass(frozen=True)
class PriceInfo:
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    currency: str = "INR"
    gst_inclusive: bool = True
    label: str = ""
    release_number: int | None = None


Provider = Callable[[Sequence], Mapping[int, PriceInfo | None]]


def default_provider(components: Sequence) -> Mapping[int, PriceInfo | None]:
    return {}


_provider: Provider = default_provider


def register(provider: Provider) -> Provider:
    """Install ``provider`` (the last registration wins). Usable as a decorator."""
    global _provider
    _provider = provider
    return provider


def reset() -> None:
    """Back to the default provider (tests)."""
    register(default_provider)


def current_provider() -> Provider:
    return _provider


def prices_for(components: Sequence) -> dict[int, PriceInfo | None]:
    if not components:
        return {}
    try:
        result = _provider(list(components)) or {}
    except Exception:  # noqa: BLE001 - the website must render without prices rather than fail
        logger.exception("catalog price provider failed")
        return {}
    return {component.pk: result.get(component.pk) for component in components}


def format_inr(amount: Decimal) -> str:
    """``₹1,40,300`` — Indian digit grouping, rupees only (paise dropped when zero)."""
    quantized = amount.quantize(Decimal("0.01"))
    rupees, paise = divmod(abs(quantized), 1)
    digits = str(int(rupees))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) if groups else tail
    if paise:
        text += f".{int(paise * 100):02d}"
    return f"{'-' if quantized < 0 else ''}₹{text}"


def price_label(info: PriceInfo | None, fallback: str = "") -> str:
    """The label shown on the website: the provider's label, else its range, else ``fallback`` (the profile field)."""
    if info is None:
        return fallback
    if info.label:
        return info.label
    low, high = info.min_amount, info.max_amount
    if low is not None and high is not None and low != high:
        return f"{format_inr(low)} - {format_inr(high)}"
    if low is not None or high is not None:
        return format_inr(low if low is not None else high)
    return fallback
