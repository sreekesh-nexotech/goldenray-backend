"""Stock as a catalog usage: a component still in stock somewhere cannot be deleted (``catalog.services.usage``).

Registered from ``InventoryConfig.ready`` as provider ``inventory.stock``: one reference per location where the
component's balance is not zero, labelled with the location code and whether it is in stock or negative — never the
quantity (the usage screen needs only ``catalog.view``; stock levels are ``inventory.view`` data). While the
``INVENTORY_STOCK`` flag is off the ledger is invisible and the provider reports nothing.
"""

from __future__ import annotations

from inventory.models import Balance
from inventory.services.common import stock_enabled

MAX_REFERENCES = 100


def component_stock(component) -> list[dict]:
    if not stock_enabled():
        return []
    rows = Balance.objects.filter(component=component, location__deleted_at__isnull=True).exclude(qty=0).select_related("location").order_by("location__code")[:MAX_REFERENCES]
    return [
        {
            "object_type": "inventory.location",
            "object_uid": row.location.uid,
            # No quantity: the usage screen needs only catalog.view, stock levels are inventory.view data.
            "label": f"{row.location.code} ({'negative balance' if row.qty < 0 else 'in stock'})",
            "status": "NEGATIVE" if row.qty < 0 else "IN_STOCK",
        }
        for row in rows
    ]


def register() -> None:
    from catalog.services import usage

    usage.register("inventory.stock")(component_stock)
