"""Company URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``company/`` (``company/profile/``, ``company/bank-accounts/``), ``settings/integrations/``; public ``company/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from company.views.bank_accounts import BankAccountViewSet
from company.views.integrations import IntegrationsView
from company.views.profile import CompanyProfileView, PublicCompanyView

router = SimpleRouter(trailing_slash=True)
router.register("company/bank-accounts", BankAccountViewSet, basename="company-bank-accounts")

staff_urlpatterns = [
    path("company/profile/", CompanyProfileView.as_view(), name="company-profile"),
    path("settings/integrations/", IntegrationsView.as_view(), name="settings-integrations"),
    *router.urls,
]
public_urlpatterns = [
    path("company/", PublicCompanyView.as_view(), name="company"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
