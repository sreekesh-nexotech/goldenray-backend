"""Customers URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``customers/`` (CRUD, ``<uid>/merge/``, ``<uid>/timeline/``, ``<uid>/notes/[<note_uid>/]``).
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from customers.views.customers import CustomerViewSet
from customers.views.notes import CustomerNoteViewSet

router = SimpleRouter(trailing_slash=True)
router.register("customers", CustomerViewSet, basename="customers")

staff_urlpatterns = [
    path("customers/<uuid:customer_uid>/notes/", CustomerNoteViewSet.as_view({"get": "list", "post": "create"}), name="customer-notes"),
    path("customers/<uuid:customer_uid>/notes/<uuid:note_uid>/", CustomerNoteViewSet.as_view({"patch": "partial_update", "delete": "destroy"}), name="customer-note-detail"),
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
