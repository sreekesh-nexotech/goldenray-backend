"""Inventory URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``inventory/`` — ``inventory/locations/`` (CRUD), ``inventory/movements/`` (list + POST, append-only),
``inventory/balances/`` (GET). Every route answers 404 while the ``INVENTORY_STOCK`` flag is off.
"""

from rest_framework.routers import SimpleRouter

from inventory.views.locations import LocationViewSet
from inventory.views.stock import BalanceViewSet, MovementViewSet

router = SimpleRouter(trailing_slash=True)
router.register("inventory/locations", LocationViewSet, basename="inventory-locations")
router.register("inventory/movements", MovementViewSet, basename="inventory-movements")
router.register("inventory/balances", BalanceViewSet, basename="inventory-balances")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
