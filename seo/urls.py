"""SEO URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``seo/`` (``metadata/``, ``redirects/``, ``overview/``); public ``seo/`` (``metadata/<page>/``,
``redirects/``) and ``sitemap/`` (``entries/``, aggregated from every app's ``services/sitemap.py``).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from seo.views.public import PublicPageMetadataView, PublicRedirectViewSet, SitemapEntriesViewSet
from seo.views.staff import PageMetadataViewSet, RedirectViewSet, SeoOverviewView

router = SimpleRouter(trailing_slash=True)
router.register("seo/metadata", PageMetadataViewSet, basename="seo-metadata")
router.register("seo/redirects", RedirectViewSet, basename="seo-redirects")

staff_urlpatterns = [
    path("seo/overview/", SeoOverviewView.as_view(), name="seo-overview"),
    *router.urls,
]
public_urlpatterns = [
    path("seo/metadata/<path:page>/", PublicPageMetadataView.as_view(), name="seo-page-metadata"),
    path("seo/redirects/", PublicRedirectViewSet.as_view({"get": "list"}), name="seo-redirects"),
    path("sitemap/entries/", SitemapEntriesViewSet.as_view({"get": "list"}), name="sitemap-entries"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
