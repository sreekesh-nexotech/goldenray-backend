"""AuditMiddleware: request id (honoured / minted / echoed), trusted client IP, context lifetime."""

import uuid

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from accounts.tests.factories import DEFAULT_PASSWORD
from audit import context
from audit.middleware import AuditMiddleware, parse_request_id
from audit.models import AuditLog

pytestmark = pytest.mark.django_db
LOGIN = "/api/v1/auth/login/"


@pytest.fixture
def user(make_user):
    return make_user(email="rid@example.com")


def _login(api_client, **headers):
    return api_client.post(LOGIN, {"email": "rid@example.com", "password": DEFAULT_PASSWORD}, format="json", **headers)


def test_well_formed_request_id_is_honoured_echoed_and_recorded(api_client, user):
    request_id = str(uuid.uuid4())
    response = _login(api_client, HTTP_X_REQUEST_ID=request_id.upper())
    assert response.status_code == 200
    assert response["X-Request-ID"] == request_id
    assert str(AuditLog.objects.get(action="accounts.login").request_id) == request_id


@pytest.mark.parametrize("header", ["not-a-uuid", "x" * 100, "'; DROP TABLE audit_log; --"])
def test_malformed_request_id_is_replaced(api_client, user, header):
    response = _login(api_client, HTTP_X_REQUEST_ID=header)
    minted = response["X-Request-ID"]
    assert minted != header and uuid.UUID(minted)
    assert str(AuditLog.objects.get(action="accounts.login").request_id) == minted


def test_missing_request_id_is_minted(api_client, user):
    response = _login(api_client)
    assert str(AuditLog.objects.get(action="accounts.login").request_id) == response["X-Request-ID"]


def test_errors_carry_the_request_id_too(api_client):
    assert uuid.UUID(api_client.get("/api/v1/dashboard/")["X-Request-ID"])
    assert uuid.UUID(api_client.get("/api/v1/does-not-exist/")["X-Request-ID"])


def test_client_ip_is_resolved_through_trusted_proxies_only(api_client, user):
    _login(api_client, REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.9")
    assert AuditLog.objects.get(action="accounts.login").ip == "203.0.113.9"
    _login(api_client, REMOTE_ADDR="198.51.100.20", HTTP_X_FORWARDED_FOR="203.0.113.9")  # untrusted peer: header ignored
    assert AuditLog.objects.filter(action="accounts.login").order_by("-id").first().ip == "198.51.100.20"


def test_context_is_closed_after_the_response(api_client, user):
    _login(api_client)
    assert context.current().request_id is None and context.current().actor is None


def test_standalone_middleware_sets_and_clears_the_context():
    seen = {}

    def view(request):
        seen["ctx"] = (context.current().request_id, context.current().ip)
        return HttpResponse("ok")

    request = RequestFactory().get("/x", HTTP_X_REQUEST_ID="0e0e7c2c-5f8c-4b3b-9d0a-b2d8f3c1a111", REMOTE_ADDR="198.51.100.7")
    response = AuditMiddleware(view)(request)
    assert seen["ctx"] == ("0e0e7c2c-5f8c-4b3b-9d0a-b2d8f3c1a111", "198.51.100.7")
    assert response["X-Request-ID"] == "0e0e7c2c-5f8c-4b3b-9d0a-b2d8f3c1a111" and request.request_id == response["X-Request-ID"]
    assert context.current().request_id is None


def test_context_is_closed_even_when_the_view_raises():
    def view(request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        AuditMiddleware(view)(RequestFactory().get("/x"))
    assert context.current().request_id is None


def test_parse_request_id():
    assert parse_request_id(None) is None and parse_request_id(123) is None and parse_request_id("") is None
    assert parse_request_id("0E0E7C2C5F8C4B3B9D0AB2D8F3C1A111") == "0e0e7c2c-5f8c-4b3b-9d0a-b2d8f3c1a111"


def test_service_token_authentication_attaches_the_agent():
    from core.service_credentials import ServiceTokenAuthentication, issue

    credential, token = issue("AGENT", "Office PC 7")
    request = RequestFactory().get("/api/agent/v1/config", HTTP_AUTHORIZATION=f"Bearer {token}")
    with context.bind(request_id=str(uuid.uuid4())):
        principal, _ = ServiceTokenAuthentication().authenticate(request)
        assert context.current().actor is principal and context.current().actor_kind == "AGENT"
        from audit.services import record

        entry = record("devices.heartbeat")
    assert entry.actor_kind == "AGENT" and entry.actor_id is None
