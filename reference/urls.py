"""Reference data URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``reference/`` (CRUD of every list); public ``reference/`` (read-only lists + the pincode lookup).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from reference.views.public import PUBLIC_LIST_VIEWS, PublicPincodeView
from reference.views.staff import LIST_VIEWSETS, PincodeViewSet

router = SimpleRouter(trailing_slash=True)
router.register("reference/pincodes", PincodeViewSet, basename="reference-pincodes")
for key, viewset in LIST_VIEWSETS.items():
    router.register(f"reference/{key}", viewset, basename=f"reference-{key}")

staff_urlpatterns = [*router.urls]
public_urlpatterns = [
    path("reference/pincodes/<str:pincode>/", PublicPincodeView.as_view(), name="reference-pincode"),
    *[path(f"reference/{key}/", view.as_view(), name=f"reference-{key}") for key, view in PUBLIC_LIST_VIEWS.items()],
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
