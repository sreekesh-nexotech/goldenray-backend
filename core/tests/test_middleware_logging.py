import json
import logging

import pytest

from flarize.logging import JsonFormatter, RequestContextFilter, request_id_var

pytestmark = pytest.mark.django_db


def test_request_id_is_generated_and_echoed(client):
    response = client.get("/healthz")
    assert len(response["X-Request-ID"]) == 36


def test_valid_incoming_request_id_is_kept(client):
    rid = "6f1d5f8e-2f7b-4a47-9a9e-5d3f1b2c3d4e"
    assert client.get("/healthz", HTTP_X_REQUEST_ID=rid)["X-Request-ID"] == rid


def test_invalid_incoming_request_id_is_replaced(client):
    response = client.get("/healthz", HTTP_X_REQUEST_ID="<script>")
    assert response["X-Request-ID"] != "<script>" and len(response["X-Request-ID"]) == 36


def test_access_log_line_is_structured(client, caplog):
    with caplog.at_level(logging.INFO, logger="flarize.request"):
        client.get("/healthz")
    record = next(record for record in caplog.records if record.name == "flarize.request")
    assert (record.method, record.path, record.status) == ("GET", "/healthz", 200)
    assert record.duration_ms >= 0


def test_json_formatter_includes_context_and_extras():
    token = request_id_var.set("abc")
    try:
        record = logging.LogRecord("flarize.test", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
        record.custom = {"a": 1}
        record.unserialisable = object()
        RequestContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "abc"
    assert payload["level"] == "WARNING"
    assert payload["custom"] == {"a": 1}
    assert payload["unserialisable"].startswith("<object object")


def test_json_formatter_renders_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("flarize.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]


# ── F-FIX: one ERROR record per server error, always carrying the request id ────────────────────────────────────────
class _Capture(logging.Handler):
    """Mirrors the stdout handler of settings.LOGGING (RequestContextFilter attached) and keeps the records."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.addFilter(RequestContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def error_records():
    handler = _Capture()
    loggers = [logging.getLogger(name) for name in ("django.request", "flarize.errors")]
    for logger in loggers:
        logger.addHandler(handler)
    yield handler.records
    for logger in loggers:
        logger.removeHandler(handler)


RID = "0b8f9c9e-6c1a-4a8c-9a55-2b1a0f4f6a11"


@pytest.mark.urls("core.tests.urls_testing")
def test_an_api_500_is_logged_once_with_the_request_id(api_client, error_records):
    assert api_client.get("/api/v1/_t/raise/unexpected/", HTTP_X_REQUEST_ID=RID).status_code == 500
    assert [(record.name, record.request_id) for record in error_records] == [("flarize.errors", RID)]


@pytest.mark.urls("core.tests.urls_testing")
def test_a_plain_view_500_is_logged_once_with_the_request_id(client, error_records):
    client.raise_request_exception = False
    response = client.get("/iclock/dev-token/_t/boom/", HTTP_X_REQUEST_ID=RID)
    assert response.status_code == 500 and response.json()["code"] == "server_error"
    assert [(record.name, record.request_id) for record in error_records] == [("django.request", RID)]
    assert error_records[0].exc_info is not None


def test_the_filter_falls_back_to_the_request_attribute():
    """django.request logs some records after RequestIdMiddleware reset the context variable."""
    record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Service Unavailable: /healthz", (), None)
    record.request = type("Request", (), {"request_id": RID})()
    RequestContextFilter().filter(record)
    assert record.request_id == RID


# ── Security review: capability tokens in URL paths never reach a log line or the error sink ───────────────────────
# Signed media URLs (reusable for 10 minutes), single-use document links, per-device terminal tokens (long-lived) and
# customer link tokens travel as path segments; the access log, the security log, SystemException.path and Django's
# own "Internal Server Error: <path>" lines wrote them out in full.
CAPABILITY_PATHS = [
    ("/api/v1/media/download/SIGNED-MEDIA-TOKEN/", "/api/v1/media/download/[redacted]/"),
    ("/api/v1/documents/download/SIGNED-DOC-TOKEN/", "/api/v1/documents/download/[redacted]/"),
    ("/iclock/DEVICE-SECRET-TOKEN/cdata", "/iclock/[redacted]/cdata"),
    ("/api/customer/v1/inspection-approvals/CUSTOMER-LINK-TOKEN/send-otp/", "/api/customer/v1/inspection-approvals/[redacted]/send-otp/"),
]


@pytest.mark.parametrize(("path", "logged"), CAPABILITY_PATHS)
def test_the_access_log_never_records_capability_tokens(client, caplog, path, logged):
    with caplog.at_level(logging.INFO, logger="flarize.request"):
        client.get(path)
    record = next(record for record in caplog.records if record.name == "flarize.request")
    assert record.path == logged


@pytest.mark.parametrize("path", ["/api/v1/users/6f1d5f8e-2f7b-4a47-9a9e-5d3f1b2c3d4e/", "/iclock/cdata", "/api/public/v1/company/", "/healthz"])
def test_ordinary_paths_are_logged_unchanged(path):
    from flarize.logging import redact_path

    assert redact_path(path) == path


def test_json_lines_redact_capability_tokens_in_messages_and_path_extras():
    record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error: %s", ("/iclock/DEVICE-SECRET-TOKEN/cdata",), None)
    record.path = "/api/v1/media/download/SIGNED-MEDIA-TOKEN/"
    line = JsonFormatter().format(record)
    assert "DEVICE-SECRET-TOKEN" not in line and "SIGNED-MEDIA-TOKEN" not in line
    payload = json.loads(line)
    assert payload["message"] == "Internal Server Error: /iclock/[redacted]/cdata" and payload["path"] == "/api/v1/media/download/[redacted]/"


def test_the_error_sink_and_the_security_log_redact_capability_tokens(caplog):
    from django.core.exceptions import TooManyFieldsSent
    from rest_framework.test import APIRequestFactory

    from core.models import SystemException
    from core.services.system_exceptions import record_exception
    from flarize.exceptions import exception_handler

    request = APIRequestFactory().get("/iclock/DEVICE-SECRET-TOKEN/cdata")
    record_exception(RuntimeError("boom"), request=request, source="api")
    assert SystemException.objects.get().path == "/iclock/[redacted]/cdata"

    with caplog.at_level(logging.WARNING, logger="flarize.security"):
        exception_handler(TooManyFieldsSent(), {"request": APIRequestFactory().post("/api/v1/documents/download/SIGNED-DOC-TOKEN/")})
    record = next(record for record in caplog.records if record.name == "flarize.security")
    assert record.path == "/api/v1/documents/download/[redacted]/"
