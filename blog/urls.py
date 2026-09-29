"""Blog URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``content/``; public ``content/`` (``sitemap/`` is the seo app's aggregate, fed by
``blog/services/sitemap.py``).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from blog.views.delivery import CollectionDeliveryView, EntryDeliveryView, EntryPreviewView
from blog.views.entries import EntryViewSet
from blog.views.schema import CollectionViewSet, TemplateAttributeSlotViewSet, TemplateImageGroupViewSet, TemplateViewSet
from blog.views.taxonomy import AuthorViewSet, BadgeViewSet, CategoryViewSet, TagViewSet

UID = "<uuid:template_uid>"

router = SimpleRouter(trailing_slash=True)
router.register("content/collections", CollectionViewSet, basename="content-collections")
router.register("content/templates", TemplateViewSet, basename="content-templates")
router.register("content/authors", AuthorViewSet, basename="content-authors")
router.register("content/categories", CategoryViewSet, basename="content-categories")
router.register("content/tags", TagViewSet, basename="content-tags")
router.register("content/badges", BadgeViewSet, basename="content-badges")
router.register("content/entries", EntryViewSet, basename="content-entries")

_LIST = {"get": "list", "post": "create"}
_DETAIL = {"get": "retrieve", "patch": "partial_update", "delete": "destroy"}

staff_urlpatterns = [
    path(f"content/templates/{UID}/image-groups/", TemplateImageGroupViewSet.as_view(_LIST), name="content-template-image-groups-list"),
    path(f"content/templates/{UID}/image-groups/<uuid:uid>/", TemplateImageGroupViewSet.as_view(_DETAIL), name="content-template-image-groups-detail"),
    path(f"content/templates/{UID}/attribute-slots/", TemplateAttributeSlotViewSet.as_view(_LIST), name="content-template-attribute-slots-list"),
    path(f"content/templates/{UID}/attribute-slots/<uuid:uid>/", TemplateAttributeSlotViewSet.as_view(_DETAIL), name="content-template-attribute-slots-detail"),
    *router.urls,
]
public_urlpatterns = [
    # The preview route comes first: ``preview`` is a reserved collection api_uid.
    path("content/preview/<str:token>/", EntryPreviewView.as_view(), name="content-preview"),
    path("content/<slug:collection>/", CollectionDeliveryView.as_view(), name="content-collection"),
    path("content/<slug:collection>/<str:slug>/", EntryDeliveryView.as_view(), name="content-entry"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
