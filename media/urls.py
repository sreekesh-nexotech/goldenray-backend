"""Media URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``media/`` (library CRUD, ``media/upload/``, ``media/<uid>/signed-url/``, ``media/download/<token>/``).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from media.views.assets import MediaAssetViewSet
from media.views.download import MediaDownloadView

router = SimpleRouter(trailing_slash=True)
router.register("media", MediaAssetViewSet, basename="media")

staff_urlpatterns = [
    path("media/download/<str:token>/", MediaDownloadView.as_view(), name="media-download"),
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
