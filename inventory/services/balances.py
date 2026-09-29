"""``GET inventory/balances/`` — stock per (component, location), read from the ``inventory_balance`` view.

Only pairs with at least one movement have a row. Rows of soft-deleted locations are left out (a location can only be
deleted with nothing in stock, so they are all zero); rows of a soft-deleted component stay visible while it still
holds stock.
"""

from __future__ import annotations

from inventory.models import Balance


def balances_queryset():
    return Balance.objects.filter(location__deleted_at__isnull=True).select_related("component__category", "location__office").order_by("component__sku", "location__code")
