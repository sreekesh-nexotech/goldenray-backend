import pytest
from django.core.cache import cache

from core.idempotency import _cache_key, _caller
from core.tests.support import IdempotentView

pytestmark = pytest.mark.urls("core.tests.urls_testing")
URL = "/api/public/v1/_t/idempotent/"
KEY = "lead-7f3a9c21-0001"


@pytest.fixture(autouse=True)
def _reset():
    IdempotentView.calls = 0


def _post(client, body, key=KEY, **extra):
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return client.post(URL, body, format="json", **headers, **extra)


def test_without_a_key_requests_run_normally(api_client):
    assert _post(api_client, {"name": "A"}, key=None).json()["call"] == 1
    assert _post(api_client, {"name": "A"}, key=None).json()["call"] == 2


def test_same_key_same_body_replays_the_stored_response(api_client):
    first = _post(api_client, {"name": "Asha"})
    second = _post(api_client, {"name": "Asha"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() == {"received": "Asha", "call": 1}
    assert second["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first
    assert IdempotentView.calls == 1


def test_same_key_different_body_is_rejected(api_client):
    _post(api_client, {"name": "Asha"})
    response = _post(api_client, {"name": "Someone else"})
    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reused"


def test_keys_are_scoped_per_caller(api_client):
    _post(api_client, {"name": "Asha"}, REMOTE_ADDR="203.0.113.1")
    other = _post(api_client, {"name": "Asha"}, REMOTE_ADDR="203.0.113.2")
    assert other.json()["call"] == 2


def test_server_errors_are_not_stored(api_client):
    assert _post(api_client, {"explode": True}).status_code == 500
    assert _post(api_client, {"explode": True}).status_code == 500
    assert IdempotentView.calls == 2


def test_in_flight_duplicate_is_409(api_client):
    from rest_framework.test import APIRequestFactory

    request = APIRequestFactory().post(URL, {"name": "Asha"}, format="json", REMOTE_ADDR="127.0.0.1")
    cache.add(f"{_cache_key('tests.leads', _caller(request), KEY)}:lock", 1, 60)
    response = _post(api_client, {"name": "Asha"})
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_in_progress"


@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 129, "bad/slash-key"])
def test_invalid_keys(api_client, key):
    response = _post(api_client, {"name": "A"}, key=key)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_idempotency_key"


def test_required_key(api_client):
    response = api_client.post("/api/public/v1/_t/idempotent-required/", {}, format="json")
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"
    assert api_client.post("/api/public/v1/_t/idempotent-required/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY).status_code == 201


def test_multipart_bodies_are_fingerprinted_by_file_metadata(api_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    def upload(content):
        return api_client.post(URL, {"name": "Asha", "resume": SimpleUploadedFile("cv.pdf", content, "application/pdf")}, format="multipart", HTTP_IDEMPOTENCY_KEY=KEY)

    assert upload(b"%PDF-1").status_code == 201
    assert upload(b"%PDF-1")["Idempotent-Replayed"] == "true"
    assert upload(b"%PDF-1-longer").status_code == 422
