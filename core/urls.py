"""core owns the staff paths ``settings/flags/`` and ``dashboard/``. ``/healthz`` is mounted by flarize/urls.py."""

from django.urls import path

from core.views.dashboard import DashboardView
from core.views.flags import FeatureFlagView

staff_urlpatterns = [
    path("settings/flags/", FeatureFlagView.as_view(), name="settings-flags"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
