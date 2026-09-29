"""Engineering URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``engineering/`` (PLAN §3.4 Engineering).
"""

from rest_framework.routers import SimpleRouter

from engineering.views.engineering import FindingViewSet, RuleSetViewSet, RunViewSet

router = SimpleRouter(trailing_slash=True)
router.register("engineering/rule-sets", RuleSetViewSet, basename="engineering-rule-sets")
router.register("engineering/runs", RunViewSet, basename="engineering-runs")
router.register("engineering/findings", FindingViewSet, basename="engineering-findings")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
