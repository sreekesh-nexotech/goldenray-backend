"""The ``PACK_RELEASE`` price source: the EMI size tiles are the packs of the current PackRelease (DV-83, DV-103).

EMI (website content) reads the release through the documented packs read ``packs.services.public.emi_size_packs``
(PLAN §1.2: "calculators read releases"). The import is static, so import-linter checks it: website content may read
configuration, while configuration may never import website content (contract ``product-master-ignores-content-and-hr``).
One tile per standard (not future-ready) on-grid pack, priced at its customer price incl. GST (a lump sum); the
per-kW figure is that price divided by the size. ``emi.apps.EmiConfig.ready`` installs this provider.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from emi.services import price_sources
from packs.services.common import CACHE_NAMESPACE as PACKS_CACHE_NAMESPACE
from packs.services.public import emi_size_packs

CACHE_NAMESPACES = (PACKS_CACHE_NAMESPACE,)  # packs bumps it when a release is published


def release_sizes() -> list[price_sources.SizeOption]:
    options = []
    for position, pack in enumerate(emi_size_packs()):
        per_kw = (pack.customer_price_incl_gst / pack.size_kw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        options.append(
            price_sources.SizeOption(
                uid=pack.key,
                label=pack.display_name,
                capacity_kw=pack.size_kw,
                price_per_kw=per_kw,
                sort_order=position,
                system_cost=pack.customer_price_incl_gst,
                created_at=pack.release.published_at,
                updated_at=pack.release.published_at,
                position=position,
            )
        )
    return options


def install() -> None:
    """Register :func:`release_sizes` as the ``PACK_RELEASE`` provider (``EmiConfig.ready``; tests after a reset)."""
    price_sources.register(release_sizes, cache_namespaces=CACHE_NAMESPACES)
