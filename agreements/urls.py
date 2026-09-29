"""Agreements URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``agreements/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from agreements.views.agreements import AgreementViewSet
from agreements.views.price_override import AgreementPriceOverrideView

router = SimpleRouter(trailing_slash=True)
router.register("agreements", AgreementViewSet, basename="agreements")

staff_urlpatterns = [
    path("agreements/<uuid:uid>/price-override/", AgreementPriceOverrideView.as_view(), name="agreements-price-override"),
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
