"""Calculators URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``calculators/`` (the sizing tables); public ``calculators/basic``, ``calculators/basic-v2``,
``calculators/advanced`` (``calculators/emi*`` belongs to the emi app).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from calculators.views.public import AdvancedCalculatorView, BasicCalculatorView, BasicV2CalculatorView
from calculators.views.staff import SIZING_VIEWSETS

router = SimpleRouter(trailing_slash=True)
for key, viewset in SIZING_VIEWSETS.items():
    router.register(f"calculators/{key}", viewset, basename=f"calculators-{key}")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [
    path("calculators/basic/", BasicCalculatorView.as_view(), name="calculators-basic"),
    path("calculators/basic-v2/", BasicV2CalculatorView.as_view(), name="calculators-basic-v2"),
    path("calculators/advanced/", AdvancedCalculatorView.as_view(), name="calculators-advanced"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
