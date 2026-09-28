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
