"""Pricing URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``pricing/`` (PLAN §3.4 Pricing). No public endpoints: the website reads prices through the catalog's
``products/`` payloads (price provider) and later the packs/calculators releases.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from pricing.views.config import CostConfigHistoryViewSet, CostConfigView, InstallationMatrixViewSet, StatutoryFeeViewSet, ValidityPolicyView
from pricing.views.market_rates import MarketRateSetViewSet
from pricing.views.offers import OfferViewSet
from pricing.views.prices import CurrentPriceViewSet, PriceViewSet
from pricing.views.releases import ReleaseViewSet

router = SimpleRouter(trailing_slash=True)
router.register("pricing/prices", PriceViewSet, basename="pricing-prices")
router.register("pricing/current", CurrentPriceViewSet, basename="pricing-current")
router.register("pricing/cost-config/history", CostConfigHistoryViewSet, basename="pricing-cost-config-history")
router.register("pricing/installation-matrix", InstallationMatrixViewSet, basename="pricing-installation-matrix")
router.register("pricing/statutory-fees", StatutoryFeeViewSet, basename="pricing-statutory-fees")
router.register("pricing/market-rate-sets", MarketRateSetViewSet, basename="pricing-market-rate-sets")
router.register("pricing/offers", OfferViewSet, basename="pricing-offers")
router.register("pricing/releases", ReleaseViewSet, basename="pricing-releases")

staff_urlpatterns = [
    path("pricing/cost-config/", CostConfigView.as_view(), name="pricing-cost-config"),
    path("pricing/validity-policy/", ValidityPolicyView.as_view(), name="pricing-validity-policy"),
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
