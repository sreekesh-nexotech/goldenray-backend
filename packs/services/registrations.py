"""What packs registers with other registries (called from ``PacksConfig.ready``).

* ``catalog.services.usage`` — ``packs.config_versions`` (a component in a pack BOM or pin of the open draft or the
  current approved version) and ``packs.current_release`` (a component in the current PackRelease's BOMs): such a
  component cannot be deleted, only retired;
* ``emi.services.price_sources`` — the ``PACK_RELEASE`` provider: one size tile per standard (not future-ready)
  on-grid pack of the current PackRelease, priced at its customer price (lump sum), cache namespace ``packs``;
* ``core.dashboard`` — counters for the ``packs`` module.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from packs.models import CURRENT_STATUSES, OPEN_STATUSES, ConfigLine, ConfigPin, ConfigVersion, ReleasePack, SystemType
from packs.services.common import CACHE_NAMESPACE
from packs.services.releases import current_release


def config_usage(component):
    statuses = (*OPEN_STATUSES, *CURRENT_STATUSES)
    versions = {}
    for version_id, number, uid, status in ConfigLine.objects.filter(component=component, pack__deleted_at__isnull=True, pack__config_version__status__in=statuses).values_list(
        "pack__config_version_id", "pack__config_version__number", "pack__config_version__uid", "pack__config_version__status"
    ):
        versions[version_id] = (number, uid, status)
    for version_id, number, uid, status in ConfigPin.objects.filter(component=component, pack__deleted_at__isnull=True, pack__config_version__status__in=statuses).values_list(
        "pack__config_version_id", "pack__config_version__number", "pack__config_version__uid", "pack__config_version__status"
    ):
        versions[version_id] = (number, uid, status)
    for number, uid, status in sorted(versions.values()):
        yield {"object_type": "packs.config_version", "object_uid": uid, "label": f"Pack config v{number}", "status": status}


def release_usage(component):
    release = current_release()
    if release is None:
        return
    if ReleasePack.objects.filter(release=release, bom__contains=[{"sku": component.sku}]).exists():
        yield {"object_type": "packs.release", "object_uid": release.uid, "label": f"PackRelease #{release.number}", "status": release.status}


@dataclass(frozen=True)
class PackSizeOption:
    """One EMI size tile; the fields of ``emi.services.price_sources.SizeOption`` (duck-typed: see :func:`register`)."""

    uid: str
    label: str
    capacity_kw: Decimal
    price_per_kw: Decimal
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    monthly_bill_reference: Decimal = Decimal("0")
    sort_order: int = 0
    system_cost: Decimal | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    position: int = 0


def emi_sizes() -> list[PackSizeOption]:
    release = current_release()
    if release is None:
        return []
    options = []
    rows = ReleasePack.objects.filter(release=release, system_type=SystemType.ONGRID, future_size_key="").order_by("size_kw", "phase", "sort_order")
    for position, pack in enumerate(rows):
        per_kw = (pack.customer_price_incl_gst / pack.size_kw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        options.append(
            PackSizeOption(
                uid=pack.key,
                label=pack.display_name,
                capacity_kw=pack.size_kw,
                price_per_kw=per_kw,
                sort_order=position,
                system_cost=pack.customer_price_incl_gst,
                created_at=release.published_at,
                updated_at=release.published_at,
                position=position,
            )
        )
    return options


def pack_counts(user) -> dict[str, int]:
    release = current_release()
    return {
        "current_release": release.number if release else 0,
        "open_drafts": ConfigVersion.objects.filter(status__in=OPEN_STATUSES).count(),
        "submitted": ConfigVersion.objects.filter(status="SUBMITTED").count(),
    }


def register() -> None:
    from catalog.services import usage
    from core import dashboard

    # emi is website content: configuration may not import it (import-linter "product-master-ignores-content-and-hr").
    # Its price-source registry is the documented seam (emi.services.price_sources: "the provider the packs package
    # registers from its AppConfig.ready()"), so packs reaches it by name at start-up and hands it duck-typed options.
    price_sources = importlib.import_module("emi.services.price_sources")
    usage.register("packs.config_versions")(config_usage)
    usage.register("packs.current_release")(release_usage)
    price_sources.register(emi_sizes, cache_namespaces=(CACHE_NAMESPACE,))
    dashboard.register("packs")(pack_counts)
