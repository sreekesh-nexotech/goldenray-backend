"""Packs URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``packs/`` (PLAN §3.4 BOM & packs); public ``packs`` (PLAN §3.3).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from packs.views.public import PublicPackDetailView, PublicPackListView
from packs.views.releases import CompareView, ReleaseViewSet
from packs.views.versions import ConfigVersionViewSet

router = SimpleRouter(trailing_slash=True)
router.register("packs/config-versions", ConfigVersionViewSet, basename="packs-config-versions")
router.register("packs/releases", ReleaseViewSet, basename="packs-releases")

staff_urlpatterns = [
    path("packs/compare/", CompareView.as_view(), name="packs-compare"),
    *router.urls,
]
public_urlpatterns = [
    path("packs/", PublicPackListView.as_view(), name="public-packs"),
    path("packs/<str:system_type>/<str:tier>/<str:size_kw>/", PublicPackDetailView.as_view(), name="public-packs-detail"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
