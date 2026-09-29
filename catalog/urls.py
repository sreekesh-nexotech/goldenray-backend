"""Catalog URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``catalog/``; public ``products/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from catalog.views.component_io import ComponentExportView, ComponentImportView
from catalog.views.components import ComponentViewSet
from catalog.views.masters import BatteryFamilyViewSet, BrandViewSet, CategoryViewSet
from catalog.views.profiles import PublicProfileViewSet
from catalog.views.public import BatteryListView, InverterListView, PanelListView, ProductDetailView

router = SimpleRouter(trailing_slash=True)
router.register("catalog/brands", BrandViewSet, basename="catalog-brands")
router.register("catalog/categories", CategoryViewSet, basename="catalog-categories")
router.register("catalog/components", ComponentViewSet, basename="catalog-components")
router.register("catalog/battery-families", BatteryFamilyViewSet, basename="catalog-battery-families")
router.register("catalog/public-profiles", PublicProfileViewSet, basename="catalog-public-profiles")

staff_urlpatterns = [
    # Before the router: "import"/"export" must not be read as a component uid (the uid regex also guards this).
    path("catalog/components/import/", ComponentImportView.as_view(), name="catalog-components-import"),
    path("catalog/components/export/", ComponentExportView.as_view(), name="catalog-components-export"),
    *router.urls,
]
public_urlpatterns = [
    path("products/panels/", PanelListView.as_view(), name="products-panels"),
    path("products/inverters/", InverterListView.as_view(), name="products-inverters"),
    path("products/batteries/", BatteryListView.as_view(), name="products-batteries"),
    path("products/<slug:category>/<slug:slug>/", ProductDetailView.as_view(), name="products-detail"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
