"""Careers URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``careers/`` (``careers/departments/``, ``careers/positions/``, ``careers/applications/``,
``careers/overview/``); public ``job-positions/``, ``job-applications/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from careers.views.applications import JobApplicationViewSet
from careers.views.departments import DepartmentViewSet
from careers.views.positions import CareersOverviewView, JobPositionViewSet
from careers.views.public import PublicJobApplicationView, PublicJobPositionDetailView, PublicJobPositionListView

router = SimpleRouter(trailing_slash=True)
router.register("careers/departments", DepartmentViewSet, basename="careers-departments")
router.register("careers/positions", JobPositionViewSet, basename="careers-positions")
router.register("careers/applications", JobApplicationViewSet, basename="careers-applications")

staff_urlpatterns = [
    path("careers/overview/", CareersOverviewView.as_view(), name="careers-overview"),
    *router.urls,
]
public_urlpatterns = [
    path("job-positions/", PublicJobPositionListView.as_view(), name="job-positions"),
    path("job-positions/<slug:slug>/", PublicJobPositionDetailView.as_view(), name="job-position"),
    path("job-applications/", PublicJobApplicationView.as_view(), name="job-applications"),
]
agent_urlpatterns: list = []
customer_urlpatterns: list = []
