"""BOM URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``bom/`` (PLAN §3.4 BOM & packs) and public ``bom/`` (the website quote, DV-4).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from bom.views.masters import FixedItemViewSet, PackageProfileViewSet, SlotViewSet, StructureItemViewSet, StructureTemplateViewSet, TemplateViewSet, TubeWeightViewSet
from bom.views.quote import BuildView, QuoteView

router = SimpleRouter(trailing_slash=True)
router.register("bom/templates", TemplateViewSet, basename="bom-templates")
router.register("bom/slots", SlotViewSet, basename="bom-slots")
router.register("bom/fixed-items", FixedItemViewSet, basename="bom-fixed-items")
router.register("bom/structure-templates", StructureTemplateViewSet, basename="bom-structure-templates")
router.register("bom/structure-items", StructureItemViewSet, basename="bom-structure-items")
router.register("bom/tube-weights", TubeWeightViewSet, basename="bom-tube-weights")
router.register("bom/package-profiles", PackageProfileViewSet, basename="bom-package-profiles")

staff_urlpatterns = [
    path("bom/build/", BuildView.as_view(), name="bom-build"),
    *router.urls,
]
public_urlpatterns = [
    path("bom/quote/", QuoteView.as_view(), name="public-bom-quote"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
