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
        assert first["Cache-Control"] == "public, max-age=120"
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
