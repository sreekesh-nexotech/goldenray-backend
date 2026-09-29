"""Legacy shim URL lists, mounted by flarize/urls.py.

Owns: ``/legacy/…`` only (``legacy_urlpatterns``) — the old website contracts (PLAN §6.2, DV-4, DV-5), flag
``LEGACY_API_SHIM``. nginx sends ``/<old path>`` to ``/legacy/<old path>`` for the groups switched to ``shim``
(deploy/nginx/legacy/groups.conf lists exactly these paths).

DRF views mounted here set ``versioning_class = None`` (``legacy.views.base.LegacyView``): under ``URLPathVersioning``
a DRF view reached without a ``version`` kwarg answers 404 by design, and legacy paths carry no version.

Main-backend paths keep the legacy ``APPEND_SLASH`` behaviour: the slash-less spelling answers 301 to the old URL with
the slash. CMS paths are routed exactly as the CMS routed them (``faqs``, ``job-positions[/<slug>]``, ``page-content``
without a slash; any other ``<slug>`` or ``<slug>/`` is a collection).
"""

from django.urls import path

from legacy.views.backend import (
    CALCULATOR_VIEWS,
    REFERENCE_VIEWS,
    BatteriesView,
    EmiCalculateView,
    EmiConfigView,
    EmiQuotationView,
    InstallationStatsView,
    MetadataView,
    SolarInvertersView,
    SolarPanelsView,
)
from legacy.views.base import AppendSlashView
from legacy.views.bom import BomCalculateView, QuotationSettingsView
from legacy.views.cms import CollectionView, FaqsView, JobPositionsView, JobPositionView, PageContentView
from legacy.views.forms import AffiliateApplicationView, JobApplicationView, LeadCollectionHomeView, SendOtpView, VerifyOtpView, WarrantyServiceRequestView

staff_urlpatterns: list = []
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []

# Main backend (/api/<x>/) — every old website URL of the inventory plus the public reads pincodes/ and tariffs/.
BACKEND_VIEWS = {
    **REFERENCE_VIEWS,
    "solar-panels": SolarPanelsView,
    "solar-inverters": SolarInvertersView,
    "batteries": BatteriesView,
    "metadata": MetadataView,
    "installation-stats": InstallationStatsView,
    **CALCULATOR_VIEWS,
    "emi-calculator": EmiCalculateView,
    "emi-calculator/config": EmiConfigView,
    "emi-calculator/quotation": EmiQuotationView,
    "lead-collection-home": LeadCollectionHomeView,
    "send-otp": SendOtpView,
    "verify-otp": VerifyOtpView,
    "affiliate-applications": AffiliateApplicationView,
    "warranty-service-requests": WarrantyServiceRequestView,
    "job-applications": JobApplicationView,
}
BOM_VIEWS = {"calculate": BomCalculateView, "quotation-settings": QuotationSettingsView}


def _slashed(prefix: str, views: dict) -> list:
    patterns = []
    for name, view in views.items():
        patterns.append(path(f"{prefix}{name}/", view.as_view(), name=f"legacy-{prefix.replace('/', '-')}{name.replace('/', '-')}"))
        patterns.append(path(f"{prefix}{name}", AppendSlashView.as_view()))
    return patterns


legacy_urlpatterns: list = [
    *_slashed("api/", BACKEND_VIEWS),
    *_slashed("bom/api/", BOM_VIEWS),
    # CMS delivery (/studio-api/api/…): the fixed routes first, then the collection catch-all, as the CMS URLconf did.
    path("studio-api/api/faqs", FaqsView.as_view(), name="legacy-cms-faqs"),
    path("studio-api/api/job-positions", JobPositionsView.as_view(), name="legacy-cms-job-positions"),
    path("studio-api/api/job-positions/<slug:slug>", JobPositionView.as_view(), name="legacy-cms-job-position"),
    path("studio-api/api/page-content", PageContentView.as_view(), name="legacy-cms-page-content"),
    path("studio-api/api/<slug:api_uid>", CollectionView.as_view(), name="legacy-cms-collection"),
    path("studio-api/api/<slug:api_uid>/", CollectionView.as_view(), name="legacy-cms-collection-slash"),
]
