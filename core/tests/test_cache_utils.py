from unittest import mock

import pytest
from django.core.cache import cache
from django.db import transaction

from core.models import SystemException
from core.tests.support import ThingsView
from flarize.cache_utils import build_key, bump, get_versions, version_key


def test_missing_versions_read_as_one():
    assert get_versions(["a:b", "c:d"]) == {"a:b": 1, "c:d": 1}


def test_bump_increments_and_is_independent_per_namespace():
    bump("a:b")
    assert get_versions(["a:b", "c:d"]) == {"a:b": 2, "c:d": 1}
    bump("a:b", "c:d")
    assert get_versions(["a:b", "c:d"]) == {"a:b": 3, "c:d": 2}


def test_bump_sets_a_ttl_on_the_version_key():
    with mock.patch.object(cache, "touch", wraps=cache.touch) as touch:
        bump("ttl:ns")
    touch.assert_called_once_with(version_key("ttl:ns"), 7 * 24 * 60 * 60)


def test_bump_validates_arguments():
    with pytest.raises(ValueError):
        bump()
    with pytest.raises(ValueError):
        bump("")


def test_build_key_changes_when_any_version_changes():
    before = build_key("resp", "/x/", versions=get_versions(["a", "b"]))
    assert before == build_key("resp", "/x/", versions=get_versions(["b", "a"]))
    bump("b")
    after = build_key("resp", "/x/", versions=get_versions(["a", "b"]))
    assert before != after
    assert after.startswith("c:resp:")


@pytest.mark.django_db
def test_bump_inside_a_transaction_bumps_again_after_commit(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with transaction.atomic():
            bump("tx:ns")
            assert get_versions(["tx:ns"]) == {"tx:ns": 2}
    assert len(callbacks) == 1
    assert get_versions(["tx:ns"]) == {"tx:ns": 3}


def test_cache_outage_is_fail_soft():
    with mock.patch.object(cache, "get_many", side_effect=ConnectionError("redis down")):
        assert get_versions(["x"]) == {"x": 1}
    with mock.patch.object(cache, "incr", side_effect=ConnectionError("redis down")):
        bump("x")  # does not raise


@pytest.mark.urls("core.tests.urls_testing")
class TestCachedPublicResponse:
    url = "/api/public/v1/_t/things/"

    def setup_method(self):
        ThingsView.calls = 0

    def test_miss_then_hit_with_headers(self, api_client):
        first = api_client.get(self.url)
        second = api_client.get(self.url)
        assert first.status_code == second.status_code == 200
        assert first["X-Cache"] == "MISS" and second["X-Cache"] == "HIT"
        assert first.json() == second.json() == {"items": [1, 2, 3], "calls": 1}
        # The server keeps the entry for its ttl (120 s, invalidated by bump()); browsers and CDNs, which bump() cannot
        # reach, may keep it for PUBLIC_CACHE_MAX_AGE_SECONDS at most (PLAN §3.1: 60 s).
        assert first["Cache-Control"] == "public, max-age=60" and second["Cache-Control"] == "public, max-age=60"
        assert first["ETag"] == second["ETag"] and first["ETag"].startswith('"')

    def test_query_string_is_part_of_the_key(self, api_client):
        api_client.get(self.url + "?a=1&b=2")
        assert api_client.get(self.url + "?b=2&a=1")["X-Cache"] == "HIT"
        assert api_client.get(self.url + "?a=2")["X-Cache"] == "MISS"

    @pytest.mark.parametrize("namespace", ["tests:things", "tests:other"])
    def test_bumping_any_dependency_invalidates(self, api_client, namespace):
        api_client.get(self.url)
        bump(namespace)
        response = api_client.get(self.url)
        assert response["X-Cache"] == "MISS"
        assert response.json()["calls"] == 2

    def test_unrelated_bump_keeps_the_entry(self, api_client):
        api_client.get(self.url)
        bump("tests:unrelated")
        assert api_client.get(self.url)["X-Cache"] == "HIT"

    def test_if_none_match_returns_304(self, api_client):
        etag = api_client.get(self.url)["ETag"]
        response = api_client.get(self.url, HTTP_IF_NONE_MATCH=etag)
        assert response.status_code == 304
        assert response.content == b""
        assert response["ETag"] == etag
        assert api_client.get(self.url, HTTP_IF_NONE_MATCH='"other"').status_code == 200

    def test_errors_are_not_cached(self, api_client):
        assert api_client.get(self.url + "?fail=1").status_code == 400
        assert api_client.get(self.url + "?fail=1").status_code == 400
        assert ThingsView.calls == 2

    def test_writes_bypass_the_cache(self, api_client):
        assert api_client.post(self.url, {}, format="json").status_code == 201

    def test_cache_read_failure_falls_back_to_the_view(self, api_client):
        with mock.patch.object(cache, "get", side_effect=ConnectionError("redis down")):
            response = api_client.get(self.url)
        assert response.status_code == 200

    @pytest.mark.django_db
    def test_declaring_no_namespace_is_a_configuration_error(self, api_client):
        response = api_client.get("/api/public/v1/_t/no-namespace/")
        assert response.status_code == 500
        assert SystemException.objects.get().exception_type == "django.core.exceptions.ImproperlyConfigured"


@pytest.mark.django_db
class TestHttpMaxAge:
    """The HTTP max-age is capped separately from the server-side TTL (F-FIX)."""

    @staticmethod
    def _serve(ttl, max_age=None):
        from rest_framework.response import Response
        from rest_framework.test import APIRequestFactory

        from flarize.cache_utils import serve_cached

        request = APIRequestFactory().get("/api/public/v1/x/")
        request.query_params = request.GET
        return serve_cached(object(), request, ["tests:max-age"], ttl, lambda: Response({"ok": True}), max_age=max_age)

    @pytest.mark.parametrize(
        ("ttl", "max_age", "expected"),
        [(300, None, 60), (120, None, 60), (30, None, 30), (300, 20, 20), (300, 600, 60), (None, None, 60)],
    )
    def test_max_age_never_exceeds_the_public_budget_or_the_server_ttl(self, ttl, max_age, expected):
        assert self._serve(ttl, max_age)["Cache-Control"] == f"public, max-age={expected}"

    def test_the_budget_is_a_setting(self, settings):
        settings.PUBLIC_CACHE_MAX_AGE_SECONDS = 45
        assert self._serve(300)["Cache-Control"] == "public, max-age=45"

    def test_the_server_side_ttl_is_unchanged(self):
        with mock.patch("flarize.cache_utils.cache.set") as cache_set:
            self._serve(300)
        assert cache_set.call_args.args[2] == 300


@pytest.mark.django_db
class TestKeyOrder:
    """content-blog WP: a cached response keeps the view's key order (byte parity of the Strapi delivery contract)."""

    @staticmethod
    def _serve():
        from rest_framework.response import Response
        from rest_framework.test import APIRequestFactory

        from flarize.cache_utils import serve_cached

        request = APIRequestFactory().get("/api/public/v1/order/")
        request.query_params = request.GET
        return serve_cached(object(), request, ["tests:order"], 60, lambda: Response({"zeta": 1, "alpha": {"b": 2, "a": 1}}))

    def test_miss_and_hit_keep_insertion_order_and_the_etag_is_canonical(self):
        import hashlib

        miss, hit = self._serve(), self._serve()
        assert miss["X-Cache"] == "MISS" and hit["X-Cache"] == "HIT"
        assert list(miss.data) == ["zeta", "alpha"] and list(hit.data["alpha"]) == ["b", "a"]
        canonical = '{"alpha":{"a":1,"b":2},"zeta":1}'
        assert miss["ETag"] == hit["ETag"] == '"' + hashlib.sha256(canonical.encode()).hexdigest()[:40] + '"'


@pytest.mark.django_db
class TestOrderedQueryKey:
    """content-blog review: a view whose result depends on parameter order opts into an order-preserving key."""

    @staticmethod
    def _serve(query: str, *, ordered: bool):
        from rest_framework.response import Response
        from rest_framework.test import APIRequestFactory

        from flarize.cache_utils import serve_cached

        request = APIRequestFactory().get(f"/api/public/v1/ordered/?{query}")
        request.query_params = request.GET
        body = {"keys": list(request.query_params.keys()), "last": request.query_params.get("v")}
        return serve_cached(object(), request, ["tests:ordered"], 60, lambda: Response(body), ordered_query=ordered)

    def test_default_key_normalises_parameter_and_value_order(self):
        assert self._serve("a=1&b=2", ordered=False)["X-Cache"] == "MISS"
        assert self._serve("b=2&a=1", ordered=False)["X-Cache"] == "HIT"

    def test_ordered_key_never_shares_an_entry_between_differently_ordered_queries(self):
        first = self._serve("sort[1]=x&sort[0]=y&v=1&v=2", ordered=True)
        assert first["X-Cache"] == "MISS" and first.data == {"keys": ["sort[1]", "sort[0]", "v"], "last": "2"}
        reordered = self._serve("sort[0]=y&sort[1]=x&v=1&v=2", ordered=True)
        assert reordered["X-Cache"] == "MISS" and reordered.data["keys"] == ["sort[0]", "sort[1]", "v"]
        values = self._serve("sort[1]=x&sort[0]=y&v=2&v=1", ordered=True)
        assert values["X-Cache"] == "MISS" and values.data["last"] == "1"
        assert self._serve("sort[1]=x&sort[0]=y&v=1&v=2", ordered=True)["X-Cache"] == "HIT"
