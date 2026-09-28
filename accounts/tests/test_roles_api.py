"""roles/ — CRUD, registry normalisation, escalation guards, system-role protection and roles/registry/."""

import uuid

import pytest

from accounts import registry
from accounts.models import Role
from accounts.services import sessions
from accounts.tests.factories import RoleFactory, UserFactory, seeded_role, super_admin_role
from audit.models import AuditLog

pytestmark = pytest.mark.django_db
URL = "/api/v1/roles/"
ADMIN_GRANTS = {"roles": "*", "catalog": "*", "customers": "*", "dashboard": ["view"]}


def detail(role, suffix=""):
    return f"{URL}{role.uid}/{suffix}"


@pytest.fixture
def admin(make_user):
    return make_user(grants=ADMIN_GRANTS, scopes={"customers": "all"})


@pytest.fixture
def client(auth_client, admin):
    return auth_client(admin)


@pytest.fixture
def editor_role():
    return RoleFactory(slug="catalog-editor", name="Catalog editor", permissions={"catalog": ["view", "edit"]})


class TestPermissions:
    def test_anonymous_is_401(self, api_client, editor_role):
        assert api_client.get(URL).status_code == 401
        assert api_client.get(f"{URL}registry/").status_code == 401
        assert api_client.delete(detail(editor_role)).status_code == 401

    @pytest.mark.parametrize(
        "method,path,needed",
        [("get", "list", "view"), ("get", "registry", "view"), ("post", "list", "create"), ("patch", "detail", "edit"), ("delete", "detail", "manage")],
    )
    def test_each_action_needs_its_permission(self, auth_client, make_user, editor_role, method, path, needed):
        client = auth_client(make_user(grants={"roles": [action for action in ("view", "create", "edit", "manage") if action != needed], "catalog": "*"}))
        target = {"list": URL, "registry": f"{URL}registry/", "detail": detail(editor_role)}[path]
        assert getattr(client, method)(target, {}, format="json").status_code == 403


class TestReadAndRegistry:
    def test_list_shape_with_user_counts(self, client, editor_role):
        UserFactory(role=editor_role)
        UserFactory(role=editor_role).soft_delete()
        body = client.get(URL, {"search": "catalog"}).json()
        assert body["count"] == 1
        row = body["results"][0]
        assert row["slug"] == "catalog-editor" and row["user_count"] == 1 and row["permissions"] == {"catalog": ["view", "edit"]}
        assert row["scopes"] == {"catalog": "all"} and "id" not in row

    def test_filter_system_roles(self, client, editor_role):
        seeded_role("hr")
        slugs = {row["slug"] for row in client.get(URL, {"is_system": "true", "page_size": 50}).json()["results"]}
        assert "hr" in slugs and "catalog-editor" not in slugs

    def test_registry(self, client):
        response = client.get(f"{URL}registry/")
        assert response.status_code == 200
        assert response.json() == registry.as_dict()
        modules = {module["key"] for group in response.json()["groups"] for module in group["modules"]}
        assert modules == set(registry.MODULES)

    def test_retrieve_unknown(self, client):
        assert client.get(f"{URL}{uuid.uuid4()}/").status_code == 404


class TestCreate:
    def test_happy_path_normalises_against_the_registry(self, client, admin):
        payload = {"slug": "sales-support", "name": "Sales support", "permissions": {"customers": ["edit", "view"], "catalog": ["view"]}, "scopes": {"customers": "owned"}}
        response = client.post(URL, payload, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["permissions"] == {"catalog": ["view"], "customers": ["view", "edit"]}  # registry order
        assert body["scopes"] == {"catalog": "all", "customers": "owned"} and body["is_system"] is False and body["user_count"] == 0
        entry = AuditLog.objects.get(action="accounts.role_created")
        assert entry.actor_id == admin.pk and entry.after["slug"] == "sales-support"

    def test_missing_scope_defaults_to_the_narrowest(self, client):
        body = client.post(URL, {"slug": "x", "name": "X", "permissions": {"customers": ["view"]}}, format="json").json()
        assert body["scopes"] == {"customers": "owned"}

    @pytest.mark.parametrize(
        "permissions,scopes,field,fragment",
        [
            ({"spaceships": ["view"]}, {}, "permissions", "spaceships: Unknown module."),
            ({"catalog": ["fly"]}, {}, "permissions", "catalog: Unknown action 'fly'."),
            ({"customers": ["view"]}, {"customers": "office"}, "scopes", "customers: Scope 'office' is not allowed"),
            ({"catalog": ["view"]}, {"customers": "all"}, "scopes", "customers: No permission is granted on this module."),
            ({"catalog": ["view"]}, {"nowhere": "all"}, "scopes", "nowhere: Unknown module."),
        ],
    )
    def test_registry_violations_are_reported_not_dropped(self, client, permissions, scopes, field, fragment):
        response = client.post(URL, {"slug": "bad", "name": "Bad", "permissions": permissions, "scopes": scopes}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert any(message.startswith(fragment) for message in response.json()["errors"][field]), response.json()
        assert not Role.objects.filter(slug="bad").exists()

    @pytest.mark.parametrize(
        "payload,field",
        [({"name": "No slug"}, "slug"), ({"slug": "Not A Slug!", "name": "x"}, "slug"), ({"slug": "ok"}, "name"), ({"slug": "ok", "name": "x", "permissions": {"catalog": "view"}}, "permissions")],
    )
    def test_shape_validation(self, client, payload, field):
        response = client.post(URL, payload, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_cannot_create_a_role_with_grants_you_do_not_hold(self, client):
        response = client.post(URL, {"slug": "x", "name": "X", "permissions": {"pricing": ["publish"]}}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "role_exceeds_own_grants"

    def test_cannot_widen_a_scope(self, auth_client, make_user):
        client = auth_client(make_user(grants={"roles": "*", "customers": ["view"]}, scopes={"customers": "owned"}))
        response = client.post(URL, {"slug": "x", "name": "X", "permissions": {"customers": ["view"]}, "scopes": {"customers": "all"}}, format="json")
        assert response.status_code == 403
        assert client.post(URL, {"slug": "y", "name": "Y", "permissions": {"customers": ["view"]}, "scopes": {"customers": "owned"}}, format="json").status_code == 201

    def test_slug_taken_and_reserved(self, client, editor_role):
        assert client.post(URL, {"slug": "catalog-editor", "name": "X"}, format="json").json()["code"] == "slug_taken"
        assert client.post(URL, {"slug": "super-admin", "name": "X"}, format="json").json()["code"] == "slug_reserved"

    def test_slug_of_a_deleted_role_is_reusable(self, client, editor_role):
        editor_role.soft_delete()
        assert client.post(URL, {"slug": "catalog-editor", "name": "Again"}, format="json").status_code == 201


class TestUpdate:
    def test_grants_change_takes_effect_for_holders_immediately(self, api_client, client, editor_role):
        holder = UserFactory(role=editor_role)
        issued = sessions.start_session(holder)
        me = api_client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {issued.access}").json()
        assert me["permissions"] == {"catalog": ["edit", "view"]}
        response = client.patch(detail(editor_role), {"permissions": {"catalog": ["view"]}, "expected_version": editor_role.version}, format="json")
        assert response.status_code == 200 and response.json()["version"] == editor_role.version + 1
        me = api_client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {issued.access}").json()
        assert me["permissions"] == {"catalog": ["view"]}
        entry = AuditLog.objects.get(action="accounts.role_updated")
        assert entry.before == {"permissions": {"catalog": ["view", "edit"]}} and entry.after == {"permissions": {"catalog": ["view"]}}

    def test_name_only(self, client, editor_role):
        response = client.patch(detail(editor_role), {"name": "Catalogue editor"}, format="json")
        assert response.status_code == 200 and response.json()["name"] == "Catalogue editor"

    def test_stale_version(self, client, editor_role):
        response = client.patch(detail(editor_role), {"name": "X", "expected_version": 9}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_cannot_edit_the_grants_of_your_own_role(self, client, admin):
        response = client.patch(detail(admin.role), {"permissions": {"roles": ["view"]}}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "own_role_locked"
        assert client.patch(detail(admin.role), {"description": "Mine"}, format="json").status_code == 200

    def test_cannot_change_a_role_holding_more_than_you(self, client):
        stronger = RoleFactory(permissions={"pricing": ["publish"]})
        assert client.patch(detail(stronger), {"name": "X"}, format="json").json()["code"] == "role_exceeds_own_grants"

    def test_cannot_add_grants_you_do_not_hold(self, client, editor_role):
        response = client.patch(detail(editor_role), {"permissions": {"catalog": ["view"], "pricing": ["view"]}}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "role_exceeds_own_grants"

    def test_system_roles_are_editable_but_their_slug_is_fixed(self, auth_client):
        boss = UserFactory(role=super_admin_role())
        client = auth_client(boss)
        hr = seeded_role("hr")
        assert client.patch(detail(hr), {"slug": "people"}, format="json").json()["code"] == "system_role_slug_locked"
        response = client.patch(detail(hr), {"permissions": {"employees": ["view"]}}, format="json")
        assert response.status_code == 200 and response.json()["permissions"] == {"employees": ["view"]}

    def test_only_a_super_admin_touches_the_super_admin_role(self, auth_client):
        admin = UserFactory(role=seeded_role("admin"))
        response = auth_client(admin).patch(detail(super_admin_role()), {"name": "God"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "super_admin_required"

    def test_registry_violations(self, client, editor_role):
        response = client.patch(detail(editor_role), {"scopes": {"catalog": "owned"}}, format="json")
        assert response.status_code == 400 and "scopes" in response.json()["errors"]

    def test_slug_change_for_custom_roles(self, client, editor_role):
        RoleFactory(slug="taken")
        assert client.patch(detail(editor_role), {"slug": "taken"}, format="json").json()["code"] == "slug_taken"
        assert client.patch(detail(editor_role), {"slug": "catalogue-editor"}, format="json").json()["slug"] == "catalogue-editor"


class TestDelete:
    def test_unused_custom_role(self, client, editor_role):
        assert client.delete(detail(editor_role)).status_code == 204
        assert Role.all_objects.get(pk=editor_role.pk).deleted_at is not None
        assert AuditLog.objects.get(action="accounts.role_deleted").before["slug"] == "catalog-editor"

    def test_role_in_use_is_409(self, client, editor_role):
        UserFactory(role=editor_role)
        response = client.delete(detail(editor_role))
        assert response.status_code == 409 and response.json()["code"] == "role_in_use"

    def test_role_of_a_deleted_user_only_is_deletable(self, client, editor_role):
        UserFactory(role=editor_role).soft_delete()
        assert client.delete(detail(editor_role)).status_code == 204

    def test_system_role_is_protected(self, auth_client):
        client = auth_client(UserFactory(role=super_admin_role()))
        response = client.delete(detail(seeded_role("procurement")))
        assert response.status_code == 409 and response.json()["code"] == "system_role_protected"

    def test_stale_version(self, client, editor_role):
        assert client.delete(f"{detail(editor_role)}?expected_version=7").json()["code"] == "stale_version"
