"""Inventory counters for ``GET /api/<version>/dashboard/`` (registered from ``InventoryConfig.ready`` behind the
``INVENTORY_STOCK`` flag: the dashboard leaves the module out while the flag is off).

``inventory`` supports only the ``all`` scope; the counters still go through ``core.scopes`` (fail closed).
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from core import scopes
from inventory.models import Balance, Location, Movement
from inventory.services.common import FLAG

RECENT_DAYS = 7


def inventory_counts(user) -> dict[str, int]:
    balances = scopes.apply(Balance.objects.filter(location__deleted_at__isnull=True), user, "inventory")
    totals = balances.aggregate(
        stocked_components=Count("component", filter=Q(qty__gt=0), distinct=True),
        negative_balances=Count("component", filter=Q(qty__lt=0)),
    )
    totals["locations"] = scopes.apply(Location.objects.all(), user, "inventory").count()
    since = timezone.now() - timedelta(days=RECENT_DAYS)
    totals[f"movements_last_{RECENT_DAYS}_days"] = scopes.apply(Movement.objects.filter(at__gte=since), user, "inventory").count()
    return totals


def register() -> None:
    from core import dashboard

    dashboard.register("inventory", flag=FLAG)(inventory_counts)
