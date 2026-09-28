"""GET audit/ — permission, shape, filters and cursor pagination."""

import uuid
from datetime import datetime
from datetime import timezone as dt_timezone

import pytest

from audit.models import AuditLog
from audit.services import record

pytestmark = pytest.mark.django_db
URL = "/api/v1/audit/"


@pytest.fixture
def auditor(make_user):
    return make_user(grants={"audit": ["view"]}, email="auditor@example.com", first_name="Audi", last_name="Tor")


@pytest.fixture
def client(auth_client, auditor):
    return auth_client(auditor)


def _at(text):
    return datetime.fromisoformat(text).replace(tzinfo=dt_timezone.utc)


@pytest.fixture
def rows(auditor):
    subject = uuid.uuid4()
    created = []
    for at, action, actor, object_uid in (
        ("2026-08-01T10:00:00", "accounts.user_created", auditor, subject),
        ("2026-08-15T10:00:00", "accounts.user_updated", auditor, subject),
        ("2026-09-01T10:00:00", "catalog.component_created", None, uuid.uuid4()),
        ("2026-09-20T23:30:00", "accounts.role_created", auditor, uuid.uuid4()),
    ):
        entry = AuditLog(at=_at(at), action=action, actor=actor, actor_kind="USER" if actor else "SYSTEM", object_type=action.split(".")[0] + ".thing", object_uid=object_uid)
        entry.save()
        created.append(entry)
    return subject, created


def _actions(response):
    return [row["action"] for row in response.json()["results"]]


def test_anonymous_is_401(api_client):
    assert api_client.get(URL).status_code == 401


def test_needs_audit_view(auth_client, make_user):
    response = auth_client(make_user(grants={"users": ["view"]})).get(URL)
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"


def test_writes_are_denied(client):
    """Only `list` is mapped; anything else is refused by default deny before a handler could run."""
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(URL, {}, format="json").status_code == 403


def test_shape_newest_first_without_internal_ids(client, rows, auditor):
    response = client.get(URL, {"action": "accounts.*"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"results", "next", "previous"}
    assert _actions(response)[:3] == ["accounts.role_created", "accounts.user_updated", "accounts.user_created"]
    row = body["results"][0]
    assert set(row) == {"at", "action", "actor", "actor_kind", "request_id", "ip", "object_type", "object_uid", "before", "after", "note"}
    assert row["actor"] == {"uid": str(auditor.uid), "email": "auditor@example.com", "name": "Audi Tor"}
    assert client.get(URL, {"action": "catalog.component_created"}).json()["results"][0]["actor"] is None


def test_filters(client, rows, auditor):
    subject, _ = rows
    assert _actions(client.get(URL, {"object_type": "accounts.thing", "object_uid": str(subject)})) == ["accounts.user_updated", "accounts.user_created"]
    assert set(_actions(client.get(URL, {"actor": str(auditor.uid), "to": "2026-09-30"}))) == {"accounts.user_created", "accounts.user_updated", "accounts.role_created"}
    assert _actions(client.get(URL, {"filter[action]": "catalog.component_created"})) == ["catalog.component_created"]
    assert _actions(client.get(URL, {"from": "2026-08-10", "to": "2026-09-01"})) == ["catalog.component_created", "accounts.user_updated"]
    assert _actions(client.get(URL, {"from": "2026-09-20T23:00:00+00:00", "to": "2026-09-20T23:59:59Z"})) == ["accounts.role_created"]
    # A '+' left unencoded in the query string arrives as a space.
    assert _actions(client.get(f"{URL}?from=2026-09-20T23:00:00 00:00&to=2026-09-21")) == ["accounts.role_created"]


@pytest.mark.parametrize("params", [{"from": "yesterday"}, {"to": "2026-13-45"}, {"object_uid": "nope"}, {"actor": "nope"}])
def test_invalid_filters_are_400(client, params):
    response = client.get(URL, params)
    assert response.status_code == 400 and response.json()["code"] == "validation_error"


def test_cursor_pagination_is_stable(client, rows):
    first = client.get(URL, {"page_size": 2, "to": "2026-09-30"})
    assert len(first.json()["results"]) == 2 and first.json()["next"]
    record("accounts.inserted_meanwhile")  # newer rows never shift the pages after the cursor
    second = client.get(first.json()["next"])
    seen = _actions(first) + _actions(second)
    assert seen == ["accounts.role_created", "catalog.component_created", "accounts.user_updated", "accounts.user_created"]


def test_viewing_writes_nothing(client, rows):
    before = AuditLog.objects.count()
    assert client.get(URL).status_code == 200
    assert AuditLog.objects.count() == before
