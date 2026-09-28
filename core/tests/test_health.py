from datetime import timedelta

import pytest
from django.utils import timezone

from core import health
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def extra_check():
    """Register a check for one test; a real check it shadows (e.g. documents' render_queue) is restored after."""
    saved = health.registered_checks()
    names = []

    def _add(name, fn, critical=True):
        health.register(name, critical=critical)(fn)
        names.append(name)

    yield _add
    for name in names:
        if name in saved:
            original, original_critical = saved[name]
            health.register(name, critical=original_critical)(original)
        else:
            health.unregister(name)


def test_all_checks_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "cache", "outbox", "render_queue", "audit_partitions"}
    assert body["checks"]["outbox"] == {
        "ok": True,
        "pending": 0,
        "oldest_age_seconds": 0,
        "stale_claims": 0,
        "retrying": 0,
        "parked": 0,
        "critical": False,
        "duration_ms": body["checks"]["outbox"]["duration_ms"],
    }
    assert response["Cache-Control"].startswith("max-age=0")


def test_only_safe_methods(client):
    response = client.post("/healthz")
    assert response.status_code == 405 and response["Content-Type"] == "application/json" and response["Allow"] == "GET, HEAD"
    assert response.json() == {"code": "method_not_allowed", "message": "Method not allowed.", "errors": {}, "error_codes": ["method_not_allowed"]}
    assert client.head("/healthz").status_code == 200


def test_outbox_lag_degrades_without_failing(client):
    event = OutboxEvent.objects.create(event_type="tests.stuck")
    OutboxEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["outbox"]["ok"] is False


def test_an_expired_outbox_claim_degrades(client):
    """A drainer that died mid-batch leaves an expired lease: the backlog must not look clean (F-FIX)."""
    event = OutboxEvent.objects.create(event_type="tests.orphaned")
    OutboxEvent.objects.filter(pk=event.pk).update(claimed_until=timezone.now() - timedelta(seconds=1), attempts=1)
    outbox = client.get("/healthz").json()["checks"]["outbox"]
    assert outbox["ok"] is False and outbox["stale_claims"] == 1 and outbox["pending"] == 1


def test_failing_critical_check_is_503(client, extra_check):
    extra_check("broken", lambda: health.CheckResult(ok=False))
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "fail"


def test_raising_check_reports_only_the_exception_class(client, extra_check):
    def explode():
        raise ConnectionError("password=hunter2")

    extra_check("render_queue", explode, critical=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["checks"]["render_queue"]["error"] == "ConnectionError"
    assert b"hunter2" not in response.content


def test_pluggable_checks_add_details(client, extra_check):
    extra_check("render_queue", lambda: health.CheckResult(ok=True, details={"oldest_age_seconds": 3}), critical=False)
    assert client.get("/healthz").json()["checks"]["render_queue"]["oldest_age_seconds"] == 3
