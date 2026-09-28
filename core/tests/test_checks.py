"""The API-contract system checks (core.E001–E005)."""

import pytest
from django.urls import path, re_path
from rest_framework import generics, mixins

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
    assert not any("GoodView" in msg for key in ("core.E001", "core.E002", "core.E003", "core.E005") for msg in by_id.get(key, []))
