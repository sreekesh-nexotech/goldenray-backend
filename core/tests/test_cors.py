"""CORS: a configured website origin may preflight the request headers the API reads (Idempotency-Key, X-Request-ID)."""

import pytest
from django.test import override_settings

ORIGIN = "http://localhost:3003"


def _preflight(client, path, headers, origin=ORIGIN):
    return client.options(path, HTTP_ORIGIN=origin, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST", HTTP_ACCESS_CONTROL_REQUEST_HEADERS=headers)


@pytest.mark.django_db
@override_settings(CORS_ALLOWED_ORIGINS=[ORIGIN])
def test_preflight_allows_configured_origin_and_api_request_headers(client):
    response = _preflight(client, "/api/public/v1/leads/", "content-type,authorization,idempotency-key,x-request-id")

    assert response["Access-Control-Allow-Origin"] == ORIGIN
    allowed = {h.strip() for h in response["Access-Control-Allow-Headers"].split(",")}
    assert {"content-type", "authorization", "idempotency-key", "x-request-id"} <= allowed
    assert "Access-Control-Allow-Credentials" not in response


@pytest.mark.django_db
@override_settings(CORS_ALLOWED_ORIGINS=[ORIGIN])
def test_preflight_rejects_unlisted_origin(client):
    response = _preflight(client, "/api/public/v1/leads/", "content-type", origin="http://evil.example")

    assert "Access-Control-Allow-Origin" not in response
