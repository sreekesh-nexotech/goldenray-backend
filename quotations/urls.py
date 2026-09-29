"""Quotations URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``quotations/``, ``quotation-content/``; public ``testimonials``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from quotations.views.content import CampaignViewSet, ContentVersionViewSet, InclusionViewSet, TestimonialViewSet, TierNameViewSet
from quotations.views.public import PublicTestimonialListView
from quotations.views.quotations import QuotationViewSet

router = SimpleRouter(trailing_slash=True)
router.register("quotation-content/versions", ContentVersionViewSet, basename="quotation-content-versions")
router.register("quotation-content/inclusions", InclusionViewSet, basename="quotation-content-inclusions")
router.register("quotation-content/tier-names", TierNameViewSet, basename="quotation-content-tier-names")
router.register("quotation-content/testimonials", TestimonialViewSet, basename="quotation-content-testimonials")
router.register("quotation-content/campaigns", CampaignViewSet, basename="quotation-content-campaigns")
router.register("quotations", QuotationViewSet, basename="quotations")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [path("testimonials/", PublicTestimonialListView.as_view(), name="public-testimonials")]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
