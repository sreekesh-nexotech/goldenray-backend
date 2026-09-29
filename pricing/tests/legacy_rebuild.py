"""Rebuild the legacy ``bom`` pricing rows from the platform tables — the executable form of the mapping tables in
docs/decisions/pricing-procurement.md, and what the ``/legacy/bom/...`` shim and the website BOM quote engine read.

Every function takes nothing but the database (through ``core_legacy_map`` for identity) and returns rows in the
legacy column vocabulary:

* :func:`global_costs` — the ``bom.GlobalCosts`` row (``office`` from ``office_per_project``,
  ``structure_repair_pct`` back to a percentage, …);
* :func:`market_rates` — ``bom.MarketRate`` rows with ``size_rates`` in the source key order (``{"3": …, "5sp": …}``
  or ``{"rate": …}`` for upgrade paths), ``bat_config``, ``from_size`` / ``to_size``;
* :func:`offers` — ``bom.Offer`` rows (``active`` ⇔ status ACTIVE; the calculator filters the dates itself);
* :func:`catalog_item_prices` — ``bom.CatalogItem.price`` / ``per_watt`` per legacy item id (the current LIST row).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from catalog.models import Component
from core.models import LegacyMap
from pricing.models import CostConfig, MarketRate, Offer, OfferStatus, Price, PriceKind
from pricing.services.legacy_import import GLOBAL_COSTS

TYPE_BACK = {"FLAT": "flat", "PERCENT": "percent"}


def _number(value, places: int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places))


def global_costs() -> dict:
    values = {row.key: row.value for row in CostConfig.objects.filter(effective_to__isnull=True)}
    entry = LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_globalcosts").order_by("id").first()
    row = {"id": int(entry.source_id.split(":")[0]) if entry else None}
    for column, (key, conversion) in GLOBAL_COSTS.items():
        value = values.get(key)
        if value is None:
            row[column] = None
        elif conversion == "percent":
            row[column] = _number(Decimal(str(value)) * 100, 4)
        elif conversion == "integer":
            row[column] = int(value)
        else:
            row[column] = _number(value, 2)
    first = CostConfig.objects.filter(key="install_rate", effective_to__isnull=True).first()
    row["updated_at"] = first.created_at if first else None
    return row


def _plain(amount: Decimal):
    return int(amount) if amount == amount.to_integral_value() else amount.normalize()


def market_rates() -> list[dict]:
    entries = LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_marketrate")
    by_legacy: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for entry in entries:
        legacy_id, key = entry.source_id.split(":", 1)
        by_legacy[int(legacy_id)].append((key, entry.target_id))
    rates = MarketRate.all_objects.in_bulk([target for cells in by_legacy.values() for _, target in cells])
    rows = []
    for legacy_id, cells in sorted(by_legacy.items()):
        cells = sorted(cells, key=lambda cell: (rates[cell[1]].sort_order, cell[1]))
        first = rates[cells[0][1]]
        upgrade = first.system_type == "UPGRADE"
        rows.append(
            {
                "id": legacy_id,
                "system_type": first.system_type.lower(),
                "tier": first.tier.lower(),
                "bat_config": first.battery_config,
                "from_size": first.from_size_key if upgrade else "",
                "to_size": first.size_key if upgrade else "",
                "size_rates": {key: _plain(rates[target].customer_price_incl_gst) for key, target in cells},
                "updated_at": first.created_at,
            }
        )
    return rows


def offers() -> list[dict]:
    entries = LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_offer").order_by("id")
    by_pk = Offer.all_objects.in_bulk([entry.target_id for entry in entries])
    rows = []
    for entry in sorted(entries, key=lambda item: int(item.source_id)):
        offer = by_pk[entry.target_id]
        rows.append(
            {
                "id": int(entry.source_id),
                "offer_id": offer.code,
                "name": offer.name,
                "offer_type": TYPE_BACK[offer.type],
                "value": offer.value,
                "applies_to": offer.applies_to_system.lower(),
                "applies_to_tier": offer.applies_to_tier.lower(),
                "start_date": offer.starts_on,
                "end_date": offer.ends_on,
                "active": offer.status == OfferStatus.ACTIVE,
                "created_at": offer.created_at,
            }
        )
    return rows


def catalog_item_prices() -> dict[int, dict]:
    """``{bom_catalogitem.id: {"price", "per_watt"}}`` from the current LIST row of the mapped component."""
    entries = LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_catalogitem", target_table=Component._meta.db_table)
    current = {row.component_id: row for row in Price.objects.filter(kind=PriceKind.LIST, effective_to__isnull=True)}
    result = {}
    for entry in entries:
        row = current.get(entry.target_id)
        result[int(entry.source_id)] = {"price": row.amount if row else None, "per_watt": row.per_watt if row else None}
    return result
