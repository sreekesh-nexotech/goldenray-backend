import django_filters
import pytest
from django.http import QueryDict
from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory

from core.models import FeatureFlag
from core.tests.factories import FeatureFlagFactory
from flarize.filters import FilterBackend, normalise_filter_params


def test_bracket_params_are_aliased_and_plain_params_win():
    params = normalise_filter_params(QueryDict("filter[status]=ACTIVE&filter[kind]=A&filter[kind]=B&kind=C&search=x&filter[bad-name]=1"))
    assert params.getlist("status") == ["ACTIVE"]
    assert params.getlist("kind") == ["C"]
    assert params["search"] == "x"
    assert "bad-name" not in params


class FlagFilter(django_filters.FilterSet):
    class Meta:
        model = FeatureFlag
        fields = ["key", "enabled"]


class FlagList(generics.ListAPIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    queryset = FeatureFlag.objects.order_by("key")
    filter_backends = [FilterBackend]
    filterset_class = FlagFilter

    def get_serializer(self, *args, **kwargs):
        from core.tests.support import FlagRowSerializer

        return FlagRowSerializer(*args, **kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["?enabled=true", "?filter[enabled]=true"])
def test_both_filter_styles_reach_the_filterset(query):
    FeatureFlagFactory(key="ADMS_RECEIVER", enabled=True)
    FeatureFlagFactory(key="INVENTORY_STOCK", enabled=False)
    response = FlagList.as_view()(APIRequestFactory().get(f"/api/v1/flags/{query}"), version="v1")
    assert response.status_code == 200, response.data
    assert [row["key"] for row in response.data["results"]] == ["ADMS_RECEIVER"]


def test_default_filter_backends():
    from django.conf import settings

    assert settings.REST_FRAMEWORK["DEFAULT_FILTER_BACKENDS"] == ["flarize.filters.FilterBackend", "rest_framework.filters.SearchFilter", "rest_framework.filters.OrderingFilter"]
