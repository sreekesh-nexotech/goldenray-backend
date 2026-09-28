import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.errors import DomainError, NotFound, StaleVersion
from core.flags import flag_enabled
from core.models import FeatureFlag, OutboxEvent
from core.services.flags import list_flags, set_flag
from core.tests.factories import FeatureFlagFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/settings/flags/"


def test_defaults_are_off_and_unknown_flags_are_off():
    assert all(state["enabled"] is False for state in list_flags())
    assert [state["key"] for state in list_flags()] == ["ADMS_RECEIVER", "AGREEMENTS_PRICE_OVERRIDE", "INVENTORY_STOCK", "LEGACY_API_SHIM"]
    assert flag_enabled("LEGACY_API_SHIM") is False
    assert flag_enabled("SOMETHING_ELSE") is False


def test_row_overrides_default_and_reads_are_cached():
    FeatureFlagFactory(key="LEGACY_API_SHIM", enabled=True)
    assert flag_enabled("LEGACY_API_SHIM") is True
    with CaptureQueriesContext(connection) as queries:
        assert flag_enabled("LEGACY_API_SHIM") is True
    assert len(queries) == 0


def test_soft_deleted_row_falls_back_to_default():
    row = FeatureFlagFactory(key="LEGACY_API_SHIM", enabled=True)
    row.soft_delete()
    assert flag_enabled("LEGACY_API_SHIM") is False


def test_set_flag_creates_then_versions_and_invalidates_cache(make_user):
    user = make_user()
    assert flag_enabled("ADMS_RECEIVER") is False
    row = set_flag("ADMS_RECEIVER", enabled=True, user=user, note="terminal test")
    assert (row.enabled, row.version, row.created_by, row.note) == (True, 1, user, "terminal test")
    assert flag_enabled("ADMS_RECEIVER") is True
    row = set_flag("ADMS_RECEIVER", enabled=False, user=user, expected_version=1)
    assert (row.enabled, row.version, row.updated_by) == (False, 2, user)
    assert flag_enabled("ADMS_RECEIVER") is False
    events = list(OutboxEvent.objects.filter(event_type="core.flag_changed").order_by("id").values_list("payload", flat=True))
    assert events == [{"key": "ADMS_RECEIVER", "enabled": True, "previous": False}, {"key": "ADMS_RECEIVER", "enabled": False, "previous": True}]


def test_unchanged_value_does_not_bump_version_or_emit():
    set_flag("INVENTORY_STOCK", enabled=True, user=None)
    row = set_flag("INVENTORY_STOCK", enabled=True, user=None)
    assert row.version == 1
    assert OutboxEvent.objects.filter(event_type="core.flag_changed").count() == 1


def test_set_flag_errors():
    with pytest.raises(NotFound) as excinfo:
        set_flag("NOPE", enabled=True, user=None)
    assert excinfo.value.code == "unknown_flag"
    with pytest.raises(DomainError):
        set_flag("ADMS_RECEIVER", enabled="yes", user=None)
    set_flag("ADMS_RECEIVER", enabled=True, user=None)
    with pytest.raises(StaleVersion):
        set_flag("ADMS_RECEIVER", enabled=False, user=None, expected_version=9)


@pytest.mark.urls("core.tests.urls_testing")
class TestGating:
    def test_drf_view_is_404_while_off_even_for_anonymous(self, api_client, make_user, auth_client):
        assert api_client.get("/api/v1/_t/gated/").status_code == 404
        client = auth_client(make_user(grants={"settings": ["view"]}))
        assert client.get("/api/v1/_t/gated/").status_code == 404
        set_flag("LEGACY_API_SHIM", enabled=True, user=None)
        assert api_client.get("/api/v1/_t/gated/").status_code == 401
        assert client.get("/api/v1/_t/gated/").json() == {"gated": True}

    def test_plain_view_decorator(self, client):
        assert client.get("/iclock/device-token/_t/").status_code == 404
        set_flag("ADMS_RECEIVER", enabled=True, user=None)
        assert client.get("/iclock/device-token/_t/").json() == {"gated": True}


class TestFlagsApi:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401
        assert api_client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": True}, format="json").status_code == 401

    def test_missing_permission_is_403(self, make_user, auth_client):
        client = auth_client(make_user(grants={"dashboard": ["view"]}))
        assert client.get(URL).status_code == 403
        viewer = auth_client(make_user(grants={"settings": ["view"]}))
        response = viewer.patch(URL, {"key": "ADMS_RECEIVER", "enabled": True}, format="json")
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"

    def test_list(self, make_user, auth_client):
        FeatureFlagFactory(key="LEGACY_API_SHIM", enabled=True, note="cutover")
        body = auth_client(make_user(grants={"settings": ["view"]})).get(URL).json()
        assert [flag["key"] for flag in body] == ["ADMS_RECEIVER", "AGREEMENTS_PRICE_OVERRIDE", "INVENTORY_STOCK", "LEGACY_API_SHIM"]
        shim = body[-1]
        assert shim["enabled"] is True and shim["default"] is False and shim["note"] == "cutover" and shim["version"] == 1
        assert body[0]["uid"] is None and body[0]["version"] is None

    def test_toggle(self, make_user, auth_client):
        user = make_user(grants={"settings": ["view", "edit"]})
        client = auth_client(user)
        response = client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": True, "note": "office 1"}, format="json")
        assert response.status_code == 200
        assert response.json()["enabled"] is True and response.json()["version"] == 1
        assert FeatureFlag.objects.get(key="ADMS_RECEIVER").updated_by == user
        response = client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": False, "expected_version": 1}, format="json")
        assert response.json()["enabled"] is False and response.json()["version"] == 2

    def test_validation_error_envelope(self, make_user, auth_client):
        response = auth_client(make_user(grants={"settings": ["edit"]})).patch(URL, {"key": "ADMS_RECEIVER"}, format="json")
        assert response.status_code == 400
        assert response.json()["errors"] == {"enabled": ["This field is required."]}
        assert response.json()["error_codes"] == ["required"]

    def test_unknown_flag_is_404(self, make_user, auth_client):
        response = auth_client(make_user(grants={"settings": ["edit"]})).patch(URL, {"key": "NOPE", "enabled": True}, format="json")
        assert response.status_code == 404
        assert response.json()["code"] == "unknown_flag"

    def test_stale_version_is_409(self, make_user, auth_client):
        client = auth_client(make_user(grants={"settings": ["edit"]}))
        client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": True}, format="json")
        client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": False, "expected_version": 1}, format="json")
        response = client.patch(URL, {"key": "ADMS_RECEIVER", "enabled": True, "expected_version": 1}, format="json")
        assert response.status_code == 409
        assert response.json()["code"] == "stale_version"

    def test_throttle_scope_is_staff(self):
        from core.views.flags import FeatureFlagView

        assert FeatureFlagView.throttle_scope == "staff"
