"""The shim surface: flag gating, unversioned DRF views, trailing-slash behaviour, refused methods, throttle scopes,
and the nginx map listing exactly the shimmed paths."""

import pytest
from django.urls import URLPattern

from core.flags import FLAGS_CACHE_NAMESPACE
from core.tests.test_deploy import SWITCH, legacy_group
from flarize.cache_utils import bump
from legacy import urls
from legacy.tests.conftest import throttled
from legacy.views.base import AppendSlashView, LegacyView
from reference.tests.factories import DeviceTypeFactory

pytestmark = pytest.mark.django_db
SHIMMED_GROUPS = {group for group in SWITCH if group != "default" and not group.endswith("_other")}


def _routes() -> list[str]:
    return [str(pattern.pattern) for pattern in urls.legacy_urlpatterns if isinstance(pattern, URLPattern)]


def _concrete(route: str) -> str:
    return "/" + route.replace("<slug:api_uid>", "articles").replace("<slug:slug>", "field-sales-executive")


def _old_path(route: str) -> str:
    """The URL the website calls for a shim route (nginx adds the /legacy prefix)."""
    return _concrete(route)


@pytest.mark.parametrize("route", _routes())
def test_every_route_is_off_while_the_flag_is_off(api_client, settings, route):
    settings.FEATURE_FLAG_DEFAULTS = {**settings.FEATURE_FLAG_DEFAULTS, "LEGACY_API_SHIM": False}
    bump(FLAGS_CACHE_NAMESPACE)
    for method in ("get", "post"):
        response = getattr(api_client, method)("/legacy" + _concrete(route))
        assert response.status_code == 404 and response.json()["code"] == "not_found", (route, method)


def test_flag_row_switches_the_surface(api_client, settings):
    from core.services.flags import set_flag

    DeviceTypeFactory()
    settings.FEATURE_FLAG_DEFAULTS = {**settings.FEATURE_FLAG_DEFAULTS, "LEGACY_API_SHIM": False}
    assert api_client.get("/legacy/api/device-types/").status_code == 404
    set_flag("LEGACY_API_SHIM", enabled=True, user=None, note="cutover C2")
    assert api_client.get("/legacy/api/device-types/").status_code == 200


def test_views_are_unversioned_anonymous_and_not_in_the_schema():
    for pattern in urls.legacy_urlpatterns:
        view = pattern.callback.cls
        assert issubclass(view, LegacyView), pattern
        assert view.versioning_class is None and view.authentication_classes == [] and view.schema is None


@pytest.mark.parametrize("route", [route for route in _routes() if route.endswith("/") and not route.startswith("studio-api")])
def test_every_old_url_is_routed_by_nginx_to_a_shim_group(route):
    assert legacy_group(_old_path(route)) in SHIMMED_GROUPS, route
    assert legacy_group(_old_path(route).rstrip("/")) in SHIMMED_GROUPS, route


@pytest.mark.parametrize("route", [route for route in _routes() if route.startswith("studio-api")])
def test_cms_routes_are_routed_by_nginx_to_a_shim_group(route):
    assert legacy_group(_old_path(route)) == "cms_content", route


@pytest.mark.parametrize(
    "uri",
    [
        "/api/solar-panels/3/",
        "/api/job-applications/7/status/",
        "/api/customer-installations/",
        "/api/solar-installations/",
        "/api/emi-admin/banks/",
        "/bom/api/quotation-testimonials/",
        "/bom/api/offers/",
        "/studio-api/api/faqs/",
        "/studio-api/admin-api/auth/login/",
    ],
)
def test_nginx_never_sends_unshimmed_urls_to_the_shim(uri):
    assert legacy_group(uri) not in SHIMMED_GROUPS, uri


@pytest.mark.parametrize("path", ["/api/device-types", "/api/metadata", "/api/emi-calculator/config", "/bom/api/quotation-settings"])
def test_slashless_urls_redirect_like_the_legacy_append_slash(api_client, path):
    response = api_client.get(f"/legacy{path}?x=1")
    assert response.status_code == 301 and response["Location"] == f"{path}/?x=1"
    assert api_client.post(f"/legacy{path}").status_code == 301


def test_append_slash_view_is_only_used_for_slashless_routes():
    for pattern in urls.legacy_urlpatterns:
        if pattern.callback.cls is AppendSlashView:
            assert not str(pattern.pattern).endswith("/")


@pytest.mark.parametrize("path", ["/api/device-types/", "/api/solar-panels/", "/api/metadata/", "/bom/api/quotation-settings/"])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_writes_on_reference_tables_are_not_shimmed(api_client, path, method):
    response = getattr(api_client, method)(f"/legacy{path}", {}, format="json")
    assert response.status_code == 405 and response.json() == {"detail": f'Method "{method.upper()}" not allowed.'}


def test_get_on_a_form_endpoint_is_405_in_the_drf_body(api_client):
    assert api_client.get("/legacy/api/lead-collection-home/").json() == {"detail": 'Method "GET" not allowed.'}


def test_unknown_legacy_path_is_the_platform_404(api_client):
    response = api_client.get("/legacy/api/customer-installations/")
    assert response.status_code == 404 and response.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "path,method,scope",
    [
        ("/api/device-types/", "get", "public_read"),
        ("/api/calculate-solar/", "post", "public_read"),
        ("/api/emi-calculator/", "post", "public_read"),
        ("/api/warranty-service-requests/", "post", "public_write"),
        ("/bom/api/calculate/", "post", "public_write"),
        ("/studio-api/api/faqs", "get", "public_read"),
    ],
)
def test_throttled_like_the_canonical_endpoints(api_client, settings, path, method, scope):
    throttled(settings, scope, "2/min")
    statuses = [getattr(api_client, method)(f"/legacy{path}", {}, format="json" if method == "post" else None).status_code for _ in range(3)]
    assert statuses[-1] == 429 and 429 not in statuses[:2], statuses


def test_malformed_json_is_the_drf_parse_error(api_client):
    response = api_client.post("/legacy/api/calculate-solar/", data="{", content_type="application/json")
    assert response.status_code == 400 and response.json()["detail"].startswith("JSON parse error")
