"""Strict versioning: nothing is routed outside the allowed surfaces, and unknown versions never resolve."""

import re

import pytest
from django.conf import settings
from django.urls import URLResolver, get_resolver
from rest_framework.views import APIView

from flarize.versioning import SURFACES, allowed_versions, surface_regex, version_group, view_class, walk_patterns

VERSION = re.escape(version_group())
ALLOWED_ROUTES = [re.compile(rf"^api/{VERSION}/"), re.compile(rf"^api/public/{VERSION}/"), re.compile(rf"^api/agent/{VERSION}/"), re.compile(rf"^api/customer/{VERSION}/")]
ALLOWED_OTHER = [
    re.compile(rf"^api/schema/{VERSION}/\$$"),
    re.compile(r"^api/docs/\\Z$"),
    re.compile(r"^healthz\\Z$"),
    re.compile(r"^iclock/\(\?P<device_token>[^)]+\)/"),
    re.compile(r"^legacy/"),
]


def _routes():
    return list(walk_patterns())


def test_settings_and_surfaces_agree_on_versions():
    assert settings.REST_FRAMEWORK["ALLOWED_VERSIONS"] == settings.API_VERSIONS == allowed_versions() == ("v1",)
    assert settings.REST_FRAMEWORK["DEFAULT_VERSIONING_CLASS"] == "rest_framework.versioning.URLPathVersioning"
    assert settings.REST_FRAMEWORK["VERSION_PARAM"] == "version"
    assert settings.REST_FRAMEWORK["DEFAULT_VERSION"] is None
    assert [surface.prefix for surface in SURFACES] == ["api/public/", "api/agent/", "api/customer/", "api/"]


def test_root_urlconf_has_only_the_allowed_top_level_entries():
    top = []
    for pattern in get_resolver().url_patterns:
        regex = pattern.pattern.regex.pattern.lstrip("^")
        top.append(regex)
    expected_surfaces = [surface_regex(surface).lstrip("^") for surface in SURFACES]
    assert top[:4] == expected_surfaces
    assert all(isinstance(pattern, URLResolver) for pattern in get_resolver().url_patterns[:4])
    assert len(top) == 9, top


def test_every_route_is_under_an_allowed_prefix():
    offenders = [route for route, _ in _routes() if not any(regex.match(route) for regex in ALLOWED_ROUTES + ALLOWED_OTHER)]
    assert offenders == []


def test_every_api_route_carries_the_version_kwarg():
    offenders = [route for route, _ in _routes() if route.startswith("api/") and not route.startswith("api/docs/") and "(?P<version>" not in route]
    assert offenders == []


def test_every_drf_view_lives_under_a_versioned_surface_or_the_schema_views():
    drf_routes = [(route, cls) for route, pattern in _routes() if isinstance(cls := view_class(pattern), type) and issubclass(cls, APIView)]
    assert drf_routes, "expected at least the core staff views"
    for route, cls in drf_routes:
        versioned = any(regex.match(route) for regex in ALLOWED_ROUTES)
        schema_views = route.startswith("api/schema/") or route.startswith("api/docs/")
        legacy = route.startswith("legacy/")
        assert versioned or schema_views or legacy, f"{cls.__name__} routed at {route}"


def test_non_drf_views_are_only_healthz_iclock_or_legacy():
    plain = [route for route, pattern in _routes() if not (isinstance(view_class(pattern), type) and issubclass(view_class(pattern), APIView))]
    assert all(route.startswith(("healthz", "iclock/", "legacy/")) for route in plain), plain


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/dashboard/",
        "/api/v0/settings/flags/",
        "/api/V1/dashboard/",
        "/api/dashboard/",
        "/api/public/v2/anything/",
        "/api/agent/v2/config/",
        "/api/customer/v2/x/",
        "/api/schema/v2/",
        "/dashboard/",
    ],
)
def test_unknown_version_or_unversioned_path_is_404_with_envelope(api_client, path):
    response = api_client.get(path)
    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Not found.", "errors": {}, "error_codes": ["not_found"]}


@pytest.mark.django_db
def test_known_version_resolves(api_client):
    assert api_client.get("/api/v1/dashboard/").status_code == 401
    assert api_client.get("/api/schema/v1/").status_code == 200


def test_swagger_ui_is_served_and_points_at_the_current_schema(api_client):
    response = api_client.get("/api/docs/")
    assert response.status_code == 200
    assert b"/api/schema/v1/" in response.content


def test_drf_views_without_a_version_are_404_by_design():
    from rest_framework.test import APIRequestFactory

    from core.tests.support import EchoVersionView

    request = APIRequestFactory().get("/somewhere/")
    assert EchoVersionView.as_view()(request).status_code == 404
    assert EchoVersionView.as_view()(request, version="v1").status_code == 200
    assert EchoVersionView.as_view()(request, version="v2").status_code == 404


@pytest.mark.urls("core.tests.urls_testing")
@pytest.mark.parametrize("prefix", ["/api/v1/_t/echo/", "/api/public/v1/_t/echo/"])
def test_request_version_is_set_from_the_url(api_client, prefix):
    response = api_client.get(prefix)
    assert response.status_code == 200
    assert response.json() == {"version": "v1"}


def test_schema_is_per_version_and_only_lists_versioned_paths(api_client, db):
    response = api_client.get("/api/schema/v1/?format=json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["paths"], "schema must list the staff endpoints"
    assert all(re.match(r"^/api/(public/|agent/|customer/)?v1/", path) for path in schema["paths"])
    assert "/api/v1/settings/flags/" in schema["paths"]
