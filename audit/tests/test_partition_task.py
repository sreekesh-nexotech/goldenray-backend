"""audit.tasks.ensure_partitions (Beat, monthly) and the audit_partitions health check."""

from datetime import date
from unittest import mock

import pytest

from audit.services import partitions
from audit.tasks import ensure_partitions

pytestmark = pytest.mark.django_db


def test_creates_upcoming_partitions_when_connected_as_owner():
    assert partitions.connected_as_owner() is True  # the test role owns the table
    existing = partitions.existing_partitions()
    result = ensure_partitions.apply(kwargs={"months": 6}).get()
    assert result["skipped"] is None
    assert set(result["created"]) == set(partitions.existing_partitions()) - existing
    assert ensure_partitions.apply(kwargs={"months": 6}).get()["created"] == []  # idempotent


def test_skips_when_not_the_owner():
    with mock.patch.object(partitions, "connected_as_owner", return_value=False), mock.patch.object(partitions, "ensure_partitions") as create:
        assert ensure_partitions.apply().get() == {"skipped": "not_owner", "created": []}
    create.assert_not_called()


def test_app_role_privileges_are_reapplied(settings):
    settings.DB_APP_ROLE = "flarize_app_test_role"
    with (
        mock.patch.object(partitions, "ensure_partitions", return_value=[]) as create,
        mock.patch.object(partitions, "apply_append_only_privileges") as close,
        mock.patch.object(partitions, "table_owner", return_value="postgres"),
    ):
        ensure_partitions.apply(kwargs={"months": 2}).get()
    create.assert_called_once_with(2, app_role="flarize_app_test_role")
    close.assert_called_once_with("flarize_app_test_role")


def test_missing_upcoming_and_health(client):
    assert partitions.missing_upcoming(2) == []
    far_future = partitions.missing_upcoming(2, today=date(2099, 1, 15))
    assert far_future == ["audit_log_y2099m01", "audit_log_y2099m02"]
    with mock.patch.object(partitions, "missing_upcoming", return_value=["audit_log_y2099m02"]):
        body = client.get("/healthz").json()
    assert body["status"] == "degraded" and body["checks"]["audit_partitions"] == {
        "ok": False,
        "missing": ["audit_log_y2099m02"],
        "critical": False,
        "duration_ms": body["checks"]["audit_partitions"]["duration_ms"],
    }
