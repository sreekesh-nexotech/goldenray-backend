"""EMI URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``emi/`` (banks, interest rules, subsidy rules, system sizes, settings); public ``calculators/emi*``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from emi.views.public import EmiCalculateView, EmiConfigView, EmiQuotationView
from emi.views.staff import ROW_VIEWSETS, EmiSettingsView

router = SimpleRouter(trailing_slash=True)
for key, viewset in ROW_VIEWSETS.items():
    router.register(f"emi/{key}", viewset, basename=f"emi-{key}")

staff_urlpatterns = [path("emi/settings/", EmiSettingsView.as_view(), name="emi-settings"), *router.urls]
public_urlpatterns = [
    path("calculators/emi/config/", EmiConfigView.as_view(), name="calculators-emi-config"),
    path("calculators/emi/quotation/", EmiQuotationView.as_view(), name="calculators-emi-quotation"),
    path("calculators/emi/", EmiCalculateView.as_view(), name="calculators-emi"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
