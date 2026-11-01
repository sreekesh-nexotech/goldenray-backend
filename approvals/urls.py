"""Approvals URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``change-requests/``.
"""

from rest_framework.routers import SimpleRouter

from approvals.views.requests import ChangeRequestViewSet

router = SimpleRouter(trailing_slash=True)
router.register("change-requests", ChangeRequestViewSet, basename="change-requests")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
