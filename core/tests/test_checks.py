"""The API-contract system checks (core.E001–E006)."""

import pytest
from django.http import JsonResponse
from django.urls import path, re_path
from rest_framework import generics, mixins
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.checks import check_view_permissions
from core.models import FeatureFlag
from core.views import BaseAPIView, BaseViewSet, ListModelMixin


class NoModuleView(BaseAPIView):
    action_permissions = {"GET": "view"}


class UnknownActionView(BaseAPIView):
    module = "catalog"
    action_permissions = {"GET": "teleport"}


class OverridesQuerysetViewSet(ListModelMixin, BaseViewSet):
    module = "catalog"
    action_permissions = {"list": "view"}

    def get_queryset(self):
        return FeatureFlag.objects.all()


class DrfWritesViewSet(mixins.CreateModelMixin, BaseViewSet):
    module = "catalog"
    action_permissions = {"create": "create"}

    def base_queryset(self):
        return FeatureFlag.objects.all()


class PlainCreateView(generics.CreateAPIView):
    queryset = FeatureFlag.objects.all()


class AuthenticatedOnlyView(APIView):
    """Staff surface, but any signed-in user passes: default deny is bypassed."""

    permission_classes = [IsAuthenticated]


def plain_staff_view(request, version):
    return JsonResponse({})


class GoodView(BaseAPIView):
    module = "catalog"
    action_permissions = {"GET": "view", "POST": ("pricing", "edit")}


urlpatterns = [
    re_path(r"^api/(?P<version>v1)/no-module/$", NoModuleView.as_view()),
    re_path(r"^api/(?P<version>v1)/unknown/$", UnknownActionView.as_view()),
    re_path(r"^api/(?P<version>v1)/override/$", OverridesQuerysetViewSet.as_view({"get": "list"})),
    re_path(r"^api/(?P<version>v1)/drf-writes/$", DrfWritesViewSet.as_view({"post": "create"})),
    re_path(r"^api/(?P<version>v1)/plain-create/$", PlainCreateView.as_view()),
    re_path(r"^api/(?P<version>v1)/good/$", GoodView.as_view()),
    re_path(r"^api/(?P<version>v1)/authenticated-only/$", AuthenticatedOnlyView.as_view()),
    re_path(r"^api/(?P<version>v1)/plain/$", plain_staff_view),
    re_path(r"^api/public/(?P<version>v1)/plain/$", plain_staff_view),
    path("rogue/", GoodView.as_view()),
]


def test_the_real_urlconf_passes():
    assert check_view_permissions() == []


@pytest.mark.urls("core.tests.test_checks")
def test_violations_are_reported():
    errors = check_view_permissions()
    by_id = {}
    for error in errors:
        by_id.setdefault(error.id, []).append(error.msg)
    assert any("NoModuleView" in msg for msg in by_id["core.E001"])
    assert any("UnknownActionView" in msg for msg in by_id["core.E002"])
    assert any("OverridesQuerysetViewSet" in msg for msg in by_id["core.E003"])
    assert any("rogue/" in msg for msg in by_id["core.E004"])
    e005 = " ".join(by_id["core.E005"])
    assert "DrfWritesViewSet.perform_create" in e005 and "PlainCreateView.perform_create" in e005
    e006 = " ".join(by_id["core.E006"])
    assert "AuthenticatedOnlyView" in e006 and "plain_staff_view" in e006 and "api/(?P<version>v1)/plain/" in e006
    assert "api/public/" not in e006  # only the staff surface requires module permissions
    assert not any("GoodView" in msg for key in ("core.E001", "core.E002", "core.E003", "core.E005", "core.E006") for msg in by_id.get(key, []))


def test_every_exemption_is_routed_on_the_staff_surface_and_documented():
    """The allow-list names real staff views, each with its reason (no silent, stale exemptions)."""
    from core.checks import STAFF_VIEWS_WITHOUT_MODULE_PERMISSION, staff_views_without_module_permission

    assert set(staff_views_without_module_permission()) == set(STAFF_VIEWS_WITHOUT_MODULE_PERMISSION)
    assert all(reason.strip() for reason in STAFF_VIEWS_WITHOUT_MODULE_PERMISSION.values())
    assert "documents.views.jobs.RenderJobDetailView" in STAFF_VIEWS_WITHOUT_MODULE_PERMISSION
