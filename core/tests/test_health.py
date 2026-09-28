from datetime import timedelta

import pytest
from django.utils import timezone

from core import health
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def extra_check():
    names = []

    def _add(name, fn, critical=True):
        health.register(name, critical=critical)(fn)
        names.append(name)

    yield _add
    for name in names:
        health.unregister(name)


def test_all_checks_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"database", "cache", "outbox"}
    assert body["checks"]["outbox"] == {"ok": True, "pending": 0, "oldest_age_seconds": 0, "parked": 0, "critical": False, "duration_ms": body["checks"]["outbox"]["duration_ms"]}
    assert response["Cache-Control"].startswith("max-age=0")


def test_only_safe_methods(client):
    assert client.post("/healthz").status_code == 405
    assert client.head("/healthz").status_code == 200


def test_outbox_lag_degrades_without_failing(client):
    event = OutboxEvent.objects.create(event_type="tests.stuck")
    OutboxEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["outbox"]["ok"] is False


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
