"""The devices OpenAPI contract: the agent uploads declare their Idempotency-Key header, the lists their filters."""

import pytest
from drf_spectacular.generators import SchemaGenerator

pytestmark = pytest.mark.django_db


@pytest.fixture(scope="module")
def paths(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        return SchemaGenerator(api_version="v1").get_schema(request=None, public=True)["paths"]


def params(paths, path, method="get", where="query") -> set[str]:
    return {parameter["name"] for parameter in paths[path][method].get("parameters", []) if parameter["in"] == where}


@pytest.mark.parametrize("path", ["/api/agent/v1/sync/users/", "/api/agent/v1/sync/attendance/"])
def test_uploads_declare_the_idempotency_key(paths, path):
    """PLAN §3.4 "Idempotency-Key on uploads": the agent protocol's replay contract belongs in the schema."""
    assert "Idempotency-Key" in params(paths, path, "post", where="header")


def test_lists_declare_their_filters(paths):
    assert {"office", "agent", "unassigned", "is_active", "adms_enabled", "identity_status", "protocol", "search", "ordering"} <= params(paths, "/api/v1/devices/")
    assert {"device", "linked", "device_state", "software_state", "active_only", "search"} <= params(paths, "/api/v1/devices/device-users/")
    assert {"serial", "kind", "device", "cursor"} <= params(paths, "/api/v1/devices/adms/requests/")
    assert {"reason", "search"} <= params(paths, "/api/v1/devices/adms/unknown-devices/")
    assert {"office", "is_active", "search"} <= params(paths, "/api/v1/devices/agents/")
    assert {"field", "confidence", "device_platform", "firmware_version"} <= params(paths, "/api/v1/devices/protocol-mappings/")
    assert {"sync_type", "status", "cursor"} <= params(paths, "/api/v1/devices/{uid}/logs/")
