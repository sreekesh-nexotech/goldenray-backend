"""FAQs URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``faqs/``, ``faq-categories/``; public ``faqs/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from faqs.views.categories import FaqCategoryViewSet
from faqs.views.faqs import FaqViewSet
from faqs.views.public import PublicFaqListView

router = SimpleRouter(trailing_slash=True)
router.register("faqs", FaqViewSet, basename="faqs")
router.register("faq-categories", FaqCategoryViewSet, basename="faq-categories")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [
    path("faqs/", PublicFaqListView.as_view(), name="public-faqs"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
