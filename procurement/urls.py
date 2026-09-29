"""Procurement URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``procurement/`` (PLAN §3.4 Procurement).
"""

from rest_framework.routers import SimpleRouter

from procurement.views.batches import BatchViewSet
from procurement.views.price_master import PriceMasterViewSet
from procurement.views.suppliers import SupplierViewSet

router = SimpleRouter(trailing_slash=True)
router.register("procurement/suppliers", SupplierViewSet, basename="procurement-suppliers")
router.register("procurement/batches", BatchViewSet, basename="procurement-batches")
router.register("procurement/price-master", PriceMasterViewSet, basename="procurement-price-master")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
