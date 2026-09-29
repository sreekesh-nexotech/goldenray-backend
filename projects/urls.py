"""Projects URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``projects/`` (CRUD, ``<uid>/lock-bom/``, ``<uid>/cost-inputs/``, ``<uid>/commission/``, ``<uid>/close/``,
``<uid>/cancel/``).
"""

from rest_framework.routers import SimpleRouter

from projects.views.projects import ProjectViewSet

router = SimpleRouter(trailing_slash=True)
router.register("projects", ProjectViewSet, basename="projects")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
