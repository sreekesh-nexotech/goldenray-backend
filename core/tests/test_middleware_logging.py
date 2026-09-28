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
