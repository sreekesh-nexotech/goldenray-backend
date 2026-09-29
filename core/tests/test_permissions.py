from types import SimpleNamespace

import pytest
from rest_framework.test import APIRequestFactory

from accounts.models import User
from core import scopes
from core.models import FeatureFlag
from core.permissions import HasModulePermission, IsServicePrincipal, required_permission
from core.service_credentials import ServicePrincipal, issue
from core.tests import support
from core.tests.factories import FeatureFlagFactory

factory = APIRequestFactory()


class _APIView:
    module = "catalog"
    action_permissions = {"GET": "view", "POST": "create", "PUT": ("pricing", "edit")}


class _ViewSet:
    module = "catalog"
    action_permissions = {"list": "view", "activate": "approve"}
    action = "list"

    def get_extra_actions(self):
        return []


@pytest.mark.parametrize(
    ("method", "expected"),
    [("get", ("catalog", "view")), ("head", ("catalog", "view")), ("post", ("catalog", "create")), ("put", ("pricing", "edit")), ("delete", None), ("options", None)],
)
def test_required_permission_for_api_views(method, expected):
    request = getattr(factory, method)("/")
    assert required_permission(request, _APIView()) == expected


@pytest.mark.parametrize(("action", "expected"), [("list", ("catalog", "view")), ("activate", ("catalog", "approve")), ("destroy", None), ("metadata", None), (None, None)])
def test_required_permission_for_viewsets(action, expected):
    view = _ViewSet()
    view.action = action
    assert required_permission(factory.get("/"), view) == expected


def test_view_without_module_is_denied():
    view = SimpleNamespace(action_permissions={"GET": "view"})
    assert required_permission(factory.get("/"), view) is None


@pytest.mark.django_db
class TestHasModulePermission:
    def _check(self, user, view=None, method="get"):
        request = getattr(factory, method)("/")
        request.user = user
        return HasModulePermission().has_permission(request, view or _APIView())

    def test_grants(self, make_user):
        assert self._check(make_user(grants={"catalog": ["view"]})) is True
        assert self._check(make_user(grants={"catalog": ["edit"]})) is False
        assert self._check(make_user(grants={"catalog": ["view"]}), method="post") is False
        assert self._check(make_user(grants={"pricing": ["edit"]}), method="put") is True

    def test_default_deny_for_unmapped_methods(self, make_user):
        user = make_user(grants={"catalog": "*"})
        assert self._check(user, method="delete") is False

    def test_no_staff_or_superuser_bypass(self, make_user):
        user = make_user(is_staff=True)
        assert not hasattr(User, "is_superuser")
        assert self._check(user) is False

    def test_inactive_user_has_no_grants(self, make_user):
        assert self._check(make_user(grants={"catalog": ["view"]}, is_active=False)) is False

    def test_unknown_registry_permission_is_denied(self, make_user):
        view = SimpleNamespace(module="catalog", action_permissions={"GET": "teleport"})
        assert self._check(make_user(grants={"catalog": "*"}), view=view) is False

    def test_service_principal_is_denied(self):
        credential, _ = issue("AGENT", "pc")
        assert self._check(ServicePrincipal(credential)) is False
        request = factory.get("/")
        request.user = ServicePrincipal(credential)
        assert IsServicePrincipal().has_permission(request, SimpleNamespace(service_kinds=("AGENT",))) is True
        assert IsServicePrincipal().has_permission(request, SimpleNamespace(service_kinds=("OTHER",))) is False


@pytest.mark.django_db
@pytest.mark.urls("core.tests.urls_testing")
class TestBaseViewSet:
    url = "/api/v1/_t/test-flags/"

    @pytest.fixture(autouse=True)
    def _owned_filter(self):
        support.SERVICE_CALLS.clear()

        def owned(queryset, user):
            return queryset.filter(created_by=user)

        previous = scopes._FILTERS.get(("customers", "owned"))  # the customers app's own filter: restored afterwards
        scopes.register("customers", "owned")(owned)
        yield
        scopes._FILTERS.pop(("customers", "owned"), None)
        if previous is not None:
            scopes._FILTERS[("customers", "owned")] = previous

    def _client(self, make_user, auth_client, grants=None, scope="all"):
        user = make_user(grants=grants or {"settings": ["view", "edit"], "customers": ["view"]}, scopes={"customers": scope})
        return user, auth_client(user)

    def test_anonymous_401_and_missing_permission_403(self, api_client, make_user, auth_client):
        assert api_client.get(self.url).status_code == 401
        _, client = self._client(make_user, auth_client, grants={"customers": ["view"]})
        assert client.get(self.url).status_code == 403

    def test_list_is_scoped_and_paginated(self, make_user, auth_client):
        user, client = self._client(make_user, auth_client, scope="owned")
        FeatureFlagFactory(key="ADMS_RECEIVER", created_by=user)
        FeatureFlagFactory(key="INVENTORY_STOCK")
        body = client.get(self.url).json()
        assert body["count"] == 1 and [row["key"] for row in body["results"]] == ["ADMS_RECEIVER"]
        _, admin = self._client(make_user, auth_client, scope="all")
        assert admin.get(self.url).json()["count"] == 2

    def test_out_of_scope_retrieve_is_404_by_uid(self, make_user, auth_client):
        _, client = self._client(make_user, auth_client, scope="owned")
        other = FeatureFlagFactory(key="INVENTORY_STOCK")
        assert client.get(f"{self.url}{other.uid}/").status_code == 404
        assert client.get(f"{self.url}{other.pk}/").status_code == 404

    def test_writes_go_through_services(self, make_user, auth_client):
        user, client = self._client(make_user, auth_client)
        created = client.post(self.url, {"key": "ADMS_RECEIVER", "enabled": True}, format="json")
        assert created.status_code == 201
        uid = created.json()["uid"]
        assert support.SERVICE_CALLS[0][0:2] == ("create", user)
        updated = client.patch(f"{self.url}{uid}/", {"enabled": False, "expected_version": 1}, format="json")
        assert updated.status_code == 200 and updated.json()["version"] == 2
        assert support.SERVICE_CALLS[1] == ("update", user, {"enabled": False}, 1)
        stale = client.patch(f"{self.url}{uid}/", {"enabled": True, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        deleted = client.delete(f"{self.url}{uid}/?expected_version=2")
        assert deleted.status_code == 204
        assert support.SERVICE_CALLS[-1] == ("destroy", user, 2)
        assert FeatureFlag.all_objects.get(uid=uid).deleted_at is not None

    def test_unmapped_actions_are_denied(self, make_user, auth_client):
        _, client = self._client(make_user, auth_client, grants={"settings": "*", "customers": "*"})
        assert client.get("/api/v1/_t/test-unmapped/").status_code == 403

    def test_api_view_method_map_and_cross_module_value(self, make_user, auth_client):
        _, client = self._client(make_user, auth_client, grants={"settings": ["view"]})
        assert client.get("/api/v1/_t/staff-method/").status_code == 200
        assert client.post("/api/v1/_t/staff-method/").status_code == 403
        assert client.delete("/api/v1/_t/staff-method/").status_code == 403
        _, auditor = self._client(make_user, auth_client, grants={"audit": ["view"]})
        assert auditor.post("/api/v1/_t/staff-method/").status_code == 200
