"""Where the EMI calculator's system sizes and prices come from (``settings.EMI_PRICE_SOURCE``, DV-76).

* ``MANUAL`` (default) — the active ``emi_system_size`` rows: the legacy tiles and per-kW prices, edited in Studio
  under ``emi/system-sizes/``. This is the transitional source until the first pack release is published.
* ``PACK_RELEASE`` — the provider the packs package registers from its ``AppConfig.ready()``::

      from emi.services import price_sources

      @price_sources.register(cache_namespaces=("packs",))
      def current_release_sizes() -> list[price_sources.SizeOption]:
          return [SizeOption(uid=…, label="3kW", capacity_kw=Decimal("3.00"), system_cost=…, …), …]

  called once per (cached) configuration snapshot; ``cache_namespaces`` are the namespaces the packs package bumps
  when a release is published, so the calculator follows the new prices at once.

EMI never imports packs (website content sits beside configuration, not above it); the registry is the seam.
A missing provider under ``PACK_RELEASE`` is refused at startup (system check ``emi.W001``) and answered with 503
``emi_prices_unavailable`` rather than silently falling back to the manual prices.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

from core.errors import DomainError
from emi.models import SystemSize

MANUAL = "MANUAL"
PACK_RELEASE = "PACK_RELEASE"
SOURCES = (MANUAL, PACK_RELEASE)


@dataclass(frozen=True)
class SizeOption:
    """One priced size tile, whatever the source (the fields of the legacy ``EmiSystemSizeSerializer``)."""

    uid: str
    label: str
    capacity_kw: Decimal
    price_per_kw: Decimal
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    monthly_bill_reference: Decimal = Decimal("0")
    sort_order: int = 0
    system_cost: Decimal | None = None  # a lump-sum price (pack releases); MANUAL prices are price_per_kw × capacity
    is_active: bool = True
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    position: int = 0


@dataclass
class _Provider:
    function: Callable[[], Sequence[SizeOption]] | None = None
    cache_namespaces: tuple[str, ...] = field(default_factory=tuple)


_provider = _Provider()


def register(function: Callable[[], Sequence[SizeOption]] | None = None, *, cache_namespaces: Sequence[str] = ()):
    """Install the ``PACK_RELEASE`` provider (the last registration wins). Usable as ``@register(...)`` or ``register(fn)``."""

    def install(fn):
        _provider.function = fn
        _provider.cache_namespaces = tuple(cache_namespaces)
        return fn

    return install(function) if function is not None else install


def reset() -> None:
    """Remove the provider (tests)."""
    _provider.function = None
    _provider.cache_namespaces = ()


def has_provider() -> bool:
    return _provider.function is not None


def source() -> str:
    return getattr(settings, "EMI_PRICE_SOURCE", MANUAL) or MANUAL


def cache_namespaces() -> tuple[str, ...]:
    """Namespaces (besides ``emi:config``) the sizes depend on."""
    return _provider.cache_namespaces if source() == PACK_RELEASE else ()


def manual_sizes() -> list[SizeOption]:
    rows = SystemSize.objects.filter(is_active=True).order_by("sort_order", "capacity_kw", "id")
    return [
        SizeOption(
            uid=str(row.uid),
            label=row.label,
            capacity_kw=row.capacity_kw,
            price_per_kw=row.price_per_kw,
            price_min=row.price_min,
            price_max=row.price_max,
            monthly_bill_reference=row.monthly_bill_reference,
            sort_order=row.sort_order,
            is_active=row.is_active,
            created_at=row.created_at,
            updated_at=row.updated_at,
            position=row.pk,
        )
        for row in rows
    ]


def active_sizes() -> list[SizeOption]:
    """The priced sizes of the configured source, in display order."""
    if source() != PACK_RELEASE:
        return manual_sizes()
    if _provider.function is None:
        raise DomainError("emi_prices_unavailable", "The EMI calculator's prices are not available right now.", status=503)
    options = sorted(_provider.function(), key=lambda option: (option.sort_order, option.capacity_kw, option.position))
    return [option for option in options if option.is_active]
