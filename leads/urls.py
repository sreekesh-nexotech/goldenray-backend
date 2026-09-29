"""Leads URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``leads/`` (``leads/``, ``leads/affiliate-applications/``, ``leads/warranty-requests/``,
``leads/installations/``); public ``otp/``, ``leads/``, ``affiliate-applications/``, ``warranty-requests/``,
``installations/`` (+ ``installations/stats/``).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from leads.views.forms import AffiliateApplicationViewSet, WarrantyRequestViewSet
from leads.views.installations import InstallationViewSet
from leads.views.leads import LeadViewSet
from leads.views.public import AffiliateSubmitView, InstallationStatsView, LeadSubmitView, OtpSendView, OtpVerifyView, PublicInstallationViewSet, WarrantySubmitView

router = SimpleRouter(trailing_slash=True)
router.register("leads/affiliate-applications", AffiliateApplicationViewSet, basename="leads-affiliate-applications")
router.register("leads/warranty-requests", WarrantyRequestViewSet, basename="leads-warranty-requests")
router.register("leads/installations", InstallationViewSet, basename="leads-installations")
router.register("leads", LeadViewSet, basename="leads")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [
    path("otp/send/", OtpSendView.as_view(), name="otp-send"),
    path("otp/verify/", OtpVerifyView.as_view(), name="otp-verify"),
    path("leads/", LeadSubmitView.as_view(), name="lead-submit"),
    path("affiliate-applications/", AffiliateSubmitView.as_view(), name="affiliate-application-submit"),
    path("warranty-requests/", WarrantySubmitView.as_view(), name="warranty-request-submit"),
    path("installations/stats/", InstallationStatsView.as_view(), name="installation-stats"),
    path("installations/", PublicInstallationViewSet.as_view({"get": "list"}), name="installations"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
