"""The error envelope {code, message, errors, error_codes} for every exception class."""

from unittest import mock

import pytest
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory

from core.models import SystemException
from flarize.exceptions import exception_handler, harvest_codes

pytestmark = [pytest.mark.urls("core.tests.urls_testing"), pytest.mark.django_db]

ENVELOPE_KEYS = {"code", "message", "errors", "error_codes"}


def _get(api_client, kind):
    return api_client.get(f"/api/v1/_t/raise/{kind}/")


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("denied", 403, "permission_denied"),
        ("stale", 409, "stale_version"),
        ("drf_denied", 403, "permission_denied"),
        ("django_denied", 403, "permission_denied"),
        ("http404", 404, "not_found"),
        ("not_authenticated", 401, "not_authenticated"),
        ("auth_failed", 401, "not_authenticated"),
        ("throttled", 429, "throttled"),
        ("custom_api", 500, "upstream_down"),
    ],
)
def test_status_and_code(api_client, kind, status, code):
    response = _get(api_client, kind)
    assert response.status_code == status
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["code"] == code
    assert body["error_codes"] == [code]
    assert body["errors"] == {}
    assert body["message"]


def test_domain_error_carries_its_status_code_and_field_errors(api_client):
    response = _get(api_client, "domain")
    assert response.status_code == 422
    assert response.json() == {"code": "quota_exceeded", "message": "Quota exceeded.", "errors": {"lines": ["Too many lines."]}, "error_codes": ["quota_exceeded"]}


def test_stale_version_message(api_client):
    body = _get(api_client, "stale").json()
    assert body["code"] == "stale_version"
    assert "changed by someone else" in body["message"]


def test_validation_error_has_field_errors_and_machine_codes(api_client):
    response = api_client.post("/api/v1/_t/validate/", {"email": "not-an-email", "age": 12}, format="json")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Invalid input."
    assert set(body["errors"]) == {"email", "age"}
    assert all(isinstance(messages, list) and messages for messages in body["errors"].values())
    assert body["error_codes"] == ["invalid", "min_value"]


def test_missing_fields_report_required(api_client):
    body = api_client.post("/api/v1/_t/validate/", {}, format="json").json()
    assert body["errors"] == {"email": ["This field is required."], "age": ["This field is required."]}
    assert body["error_codes"] == ["required"]


def test_non_field_validation_error_uses_its_message(api_client):
    body = _get(api_client, "non_field").json()
    assert body == {"code": "validation_error", "message": "Dates overlap.", "errors": {"non_field_errors": ["Dates overlap."]}, "error_codes": ["invalid"]}


def test_nested_validation_errors_keep_their_structure(api_client):
    body = _get(api_client, "nested").json()
    assert body["errors"] == {"lines": [{"qty": ["Too small."]}]}
    assert body["error_codes"] == ["min_value"]


def test_django_validation_errors_are_400(api_client):
    body = _get(api_client, "django_validation").json()
    assert body["code"] == "validation_error"
    assert body["errors"] == {"name": ["Name is taken."]}
    body = _get(api_client, "django_validation_list").json()
    assert body["errors"] == {"non_field_errors": ["Something is wrong."]}


def test_throttled_sets_retry_after(api_client):
    response = _get(api_client, "throttled")
    assert response["Retry-After"] == "13"  # DRF rounds the wait up; never advise an early retry


def test_malformed_json_is_parse_error(api_client):
    response = api_client.post("/api/v1/_t/validate/", data="{not json", content_type="application/json")
    assert response.status_code == 400
    assert response.json()["code"] == "parse_error"


def test_method_not_allowed(api_client):
    response = api_client.delete("/api/v1/_t/validate/")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_unexpected_exception_is_500_without_leaking_and_is_recorded(api_client):
    response = api_client.get("/api/v1/_t/raise/unexpected/", HTTP_X_REQUEST_ID="0b8f9c9e-6c1a-4a8c-9a55-2b1a0f4f6a11")
    assert response.status_code == 500
    assert response.json() == {"code": "server_error", "message": "Internal server error.", "errors": {}, "error_codes": ["server_error"]}
    assert b"secret internal detail" not in response.content
    assert b"Traceback" not in response.content
    row = SystemException.objects.get()
    assert row.exception_type == "builtins.RuntimeError"
    assert row.message == "secret internal detail 42"
    assert row.path == "/api/v1/_t/raise/unexpected/"
    assert row.method == "GET"
    assert str(row.request_id) == "0b8f9c9e-6c1a-4a8c-9a55-2b1a0f4f6a11"
    assert "RuntimeError" in row.traceback


def test_system_exception_writer_never_raises(api_client):
    with mock.patch("core.models.SystemException.objects.create", side_effect=RuntimeError("db down")):
        response = api_client.get("/api/v1/_t/raise/unexpected/")
    assert response.status_code == 500
    assert response.json()["code"] == "server_error"


def test_handler_can_be_called_directly_for_any_exception():
    request = APIRequestFactory().get("/")
    response = exception_handler(exceptions.NotAcceptable(), {"request": request})
    assert response.status_code == 406
    assert response.data["code"] == "not_acceptable"
    response = exception_handler(exceptions.UnsupportedMediaType("text/csv"), {"request": request})
    assert response.status_code == 415
    assert response.data["code"] == "unsupported_media_type"


def test_harvest_codes_deduplicates_in_order():
    detail = {"a": [exceptions.ErrorDetail("x", code="required")], "b": {"c": [exceptions.ErrorDetail("y", code="max_length"), exceptions.ErrorDetail("z", code="required")]}}
    assert harvest_codes(detail) == ["required", "max_length"]
    assert harvest_codes(["plain"]) == ["invalid"]
    assert harvest_codes(["plain"], default=None) == []


def test_unmatched_url_returns_json_404(api_client):
    response = api_client.get("/nope/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# ── F-FIX: Django's body-parsing SuspiciousOperation subclasses are client errors, not 500s ────────────────────────
class TestSuspiciousRequestBodies:
    """Raised while DRF parses the body; anonymous callers can send them on any public POST (login included)."""

    def test_too_many_form_fields_is_400_and_not_recorded(self, api_client, caplog):
        from urllib.parse import urlencode

        body = urlencode({f"field{i}": "x" for i in range(1200)})
        response = api_client.post("/api/v1/auth/login/", data=body, content_type="application/x-www-form-urlencoded")
        assert response.status_code == 400
        assert response.json() == {"code": "bad_request", "message": "Bad request.", "errors": {}, "error_codes": ["bad_request"]}
        assert SystemException.objects.count() == 0
        assert not [record for record in caplog.records if record.levelname in ("ERROR", "CRITICAL")]

    def test_oversized_multipart_field_is_413_and_not_recorded(self, api_client, caplog):
        response = api_client.post("/api/v1/auth/login/", {"email": "a@example.com", "password": "p" * (3 * 1024 * 1024)}, format="multipart")
        assert response.status_code == 413
        assert response.json()["code"] == "request_too_large" and set(response.json()) == ENVELOPE_KEYS
        assert SystemException.objects.count() == 0
        assert not [record for record in caplog.records if record.levelname in ("ERROR", "CRITICAL")]

    @pytest.mark.parametrize(
        ("exc", "status", "code"),
        [
            ("RequestDataTooBig", 413, "request_too_large"),
            ("TooManyFieldsSent", 400, "bad_request"),
            ("TooManyFilesSent", 400, "bad_request"),
            ("SuspiciousFileOperation", 400, "bad_request"),
            ("SuspiciousOperation", 400, "bad_request"),
        ],
    )
    def test_handler_maps_every_suspicious_operation(self, exc, status, code):
        from django.core import exceptions as django_exceptions

        request = APIRequestFactory().post("/")
        response = exception_handler(getattr(django_exceptions, exc)("details the client must not see"), {"request": request})
        assert response.status_code == status and response.data["code"] == code
        assert "details" not in response.data["message"]
        assert SystemException.objects.count() == 0
