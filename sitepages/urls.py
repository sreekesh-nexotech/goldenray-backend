"""Site pages URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``pages/``, ``career-page/``; public ``pages/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from sitepages.views.career_page import CareerPageViewSet
from sitepages.views.pages import PageViewSet
from sitepages.views.public import PublicPageByRouteView, PublicPageView

router = SimpleRouter(trailing_slash=True)
router.register("pages", PageViewSet, basename="pages")
router.register("career-page", CareerPageViewSet, basename="career-page")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [
    path("pages/", PublicPageByRouteView.as_view(), name="public-page-by-route"),
    path("pages/<slug:slug>/", PublicPageView.as_view(), name="public-page"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
