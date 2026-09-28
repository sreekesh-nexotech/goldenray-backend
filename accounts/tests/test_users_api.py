"""users/ — CRUD, deactivate/reactivate/force-reset and the escalation guards."""

import uuid

import pytest
from django.core import mail

from accounts.models import PasswordReset, User, UserSession
from accounts.services import passwords, sessions
from accounts.tests.factories import RoleFactory, UserFactory, seeded_role, super_admin_role
from audit.models import AuditLog
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db
URL = "/api/v1/users/"
ADMIN_GRANTS = {"users": "*", "dashboard": ["view"], "catalog": ["view", "edit"]}
RESET_URL = "/api/v1/auth/password/reset/"
NEW_PASSWORD = "Brand-New-Passphrase-77"


def _link_token(body: str) -> str:
    return body.split("#token=", 1)[1].split()[0]


def detail(user, suffix=""):
    return f"{URL}{user.uid}/{suffix}"


@pytest.fixture
def admin(make_user):
    return make_user(grants=ADMIN_GRANTS, email="admin@example.com")


@pytest.fixture
def client(auth_client, admin):
    return auth_client(admin)


@pytest.fixture
def member_role():
    return RoleFactory(slug="member", name="Member", permissions={"dashboard": ["view"]})


@pytest.fixture
def member(member_role):
    return UserFactory(role=member_role, email="member@example.com", first_name="Anu", last_name="Das")


class TestPermissions:
    def test_anonymous_is_401(self, api_client, member):
        for method, path in [("get", URL), ("get", detail(member)), ("post", URL), ("patch", detail(member)), ("delete", detail(member)), ("post", detail(member, "deactivate/"))]:
            assert getattr(api_client, method)(path, {}, format="json").status_code == 401, (method, path)

    @pytest.mark.parametrize(
        "method,suffix,needed",
        [
            ("get", "", "view"),
            ("patch", "", "edit"),
            ("delete", "", "archive"),
            ("post", "deactivate/", "archive"),
            ("post", "reactivate/", "archive"),
            ("post", "force-reset/", "manage"),
        ],
    )
    def test_each_action_needs_its_permission(self, auth_client, make_user, member, method, suffix, needed):
        others = [action for action in ("view", "create", "edit", "archive", "manage") if action != needed]
        client = auth_client(make_user(grants={"users": others, "dashboard": ["view"]}))
        response = getattr(client, method)(detail(member, suffix), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "permission_denied"

    def test_list_and_create_need_view_and_create(self, auth_client, make_user, member_role):
        client = auth_client(make_user(grants={"users": ["edit"]}))
        assert client.get(URL).status_code == 403
        assert client.post(URL, {"email": "n@example.com", "role": str(member_role.uid)}, format="json").status_code == 403

    def test_unmapped_methods_are_denied(self, client, member):
        """PUT (full replace) is not offered; default deny answers before any handler runs."""
        response = client.put(detail(member), {"email": "x@example.com"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "permission_denied"
        member.refresh_from_db()
        assert member.email == "member@example.com"


class TestList:
    def test_shape_filters_search_and_ordering(self, client, admin, member, member_role):
        inactive = UserFactory(role=member_role, email="zed@example.com", is_active=False)
        response = client.get(URL)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 3 and [row["email"] for row in body["results"]] == ["admin@example.com", "member@example.com", "zed@example.com"]
        row = next(row for row in body["results"] if row["email"] == "member@example.com")
        assert row["role"] == {"uid": str(member_role.uid), "slug": "member", "name": "Member"}
        assert row["full_name"] == "Anu Das" and "password" not in row and "id" not in row
        assert [row["email"] for row in client.get(URL, {"is_active": "false"}).json()["results"]] == ["zed@example.com"]
        assert {row["email"] for row in client.get(URL, {"filter[role]": str(member_role.uid)}).json()["results"]} == {"member@example.com", inactive.email}
        assert [row["email"] for row in client.get(URL, {"search": "anu"}).json()["results"]] == ["member@example.com"]
        assert client.get(URL, {"ordering": "-email"}).json()["results"][0]["email"] == "zed@example.com"

    def test_soft_deleted_users_are_not_listed(self, client, member):
        member.soft_delete()
        assert member.email not in str(client.get(URL).json())
        assert client.get(detail(member)).status_code == 404

    def test_retrieve(self, client, member):
        response = client.get(detail(member))
        assert response.status_code == 200 and response.json()["uid"] == str(member.uid)
        assert client.get(f"{URL}{uuid.uuid4()}/").status_code == 404


class TestCreate:
    def test_creates_an_account_without_a_password_and_sends_an_invitation(self, client, admin, member_role, django_capture_on_commit_callbacks):
        payload = {"email": "New.Person@Example.com", "first_name": "New", "last_name": "Person", "phone_e164": "+919876543210", "title": "CRS", "role": str(member_role.uid)}
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(URL, payload, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["email"] == "New.Person@example.com" and body["must_reset_password"] is True and body["role"]["slug"] == "member"
        user = User.objects.get(uid=body["uid"])
        assert not user.has_usable_password() and user.created_by == admin
        assert PasswordReset.objects.get(user=user).used_at is None
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["New.Person@example.com"] and "#token=" in mail.outbox[0].body
        entry = AuditLog.objects.get(action="accounts.user_created")
        assert entry.actor_id == admin.pk and entry.after["email"] == "New.Person@example.com" and entry.after["role"] == str(member_role.uid)

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"email": "bad", "role": "ROLE"}, "email"),
            ({"email": "a@example.com"}, "role"),
            ({"email": "a@example.com", "role": str(uuid.uuid4())}, "role"),
            ({"email": "a@example.com", "role": "ROLE", "phone_e164": "98765"}, "phone_e164"),
        ],
    )
    def test_validation(self, client, member_role, payload, field):
        payload = {key: (str(member_role.uid) if value == "ROLE" else value) for key, value in payload.items()}
        response = client.post(URL, payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_email_taken_case_insensitively(self, client, member, member_role):
        response = client.post(URL, {"email": "MEMBER@example.com", "role": str(member_role.uid)}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "email_taken"

    def test_cannot_assign_a_role_with_grants_you_do_not_hold(self, client):
        bigger = RoleFactory(permissions={"dashboard": ["view"], "pricing": ["view", "publish"]})
        response = client.post(URL, {"email": "x@example.com", "role": str(bigger.uid)}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "role_exceeds_own_grants"
        assert any("pricing" in message for message in response.json()["errors"]["permissions"])
        assert not User.objects.filter(email="x@example.com").exists()

    def test_cannot_assign_a_wider_scope_than_your_own(self, auth_client, make_user):
        client = auth_client(make_user(grants={"users": "*", "customers": ["view"]}, scopes={"customers": "owned"}))
        wider = RoleFactory(permissions={"customers": ["view"]}, scopes={"customers": "all"})
        response = client.post(URL, {"email": "x@example.com", "role": str(wider.uid)}, format="json")
        assert response.status_code == 403 and "wider" in str(response.json()["errors"])

    def test_only_a_super_admin_assigns_the_super_admin_role(self, auth_client):
        admin = UserFactory(role=seeded_role("admin"))
        response = auth_client(admin).post(URL, {"email": "x@example.com", "role": str(super_admin_role().uid)}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "super_admin_required"
        boss = UserFactory(role=super_admin_role())
        assert auth_client(boss).post(URL, {"email": "x@example.com", "role": str(super_admin_role().uid)}, format="json").status_code == 201


class TestUpdate:
    def test_profile_fields_with_edit_only(self, auth_client, make_user, member):
        client = auth_client(make_user(grants={"users": ["view", "edit"], "dashboard": ["view"]}))
        response = client.patch(detail(member), {"first_name": "Anupama", "title": "Lead", "expected_version": member.version}, format="json")
        assert response.status_code == 200 and response.json()["first_name"] == "Anupama" and response.json()["version"] == member.version + 1
        entry = AuditLog.objects.get(action="accounts.user_updated")
        assert entry.before == {"first_name": "Anu", "title": ""} and entry.after == {"first_name": "Anupama", "title": "Lead"}

    def test_email_and_role_need_manage(self, auth_client, make_user, member, member_role):
        client = auth_client(make_user(grants={"users": ["view", "edit"], "dashboard": ["view"]}))
        assert client.patch(detail(member), {"email": "changed@example.com"}, format="json").status_code == 403
        other = RoleFactory(permissions={"dashboard": ["view"]})
        assert client.patch(detail(member), {"role": str(other.uid)}, format="json").status_code == 403
        member.refresh_from_db()
        assert member.email == "member@example.com" and member.role == member_role

    def test_manage_changes_email_and_role(self, client, member):
        other = RoleFactory(slug="other", permissions={"catalog": ["view"]})
        response = client.patch(detail(member), {"email": "anu@example.com", "role": str(other.uid)}, format="json")
        assert response.status_code == 200 and response.json()["email"] == "anu@example.com" and response.json()["role"]["slug"] == "other"

    # Security review: reset/invitation links e-mailed to the old address stayed valid after the address changed, so
    # an invitation sent to a mistyped address let its recipient set the password of the corrected account.
    def test_email_change_voids_the_invitation_sent_to_the_old_address(self, client, api_client, member_role, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            created = client.post(URL, {"email": "typo@example.com", "role": str(member_role.uid)}, format="json").json()
        old_token = _link_token(mail.outbox[-1].body)
        mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            response = client.patch(f"{URL}{created['uid']}/", {"email": "right@example.com"}, format="json")
        assert response.status_code == 200
        refused = api_client.post(RESET_URL, {"token": old_token, "new_password": NEW_PASSWORD}, format="json")
        assert refused.status_code == 400 and refused.json()["code"] == "reset_token_invalid"
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["right@example.com"]  # the pending invitation follows the address
        assert api_client.post(RESET_URL, {"token": _link_token(mail.outbox[0].body), "new_password": NEW_PASSWORD}, format="json").status_code == 204

    def test_email_change_voids_open_reset_links_of_an_active_account(self, client, api_client, member, django_capture_on_commit_callbacks):
        issued = passwords.issue_reset(member)
        with django_capture_on_commit_callbacks(execute=True):
            assert client.patch(detail(member), {"email": "anu@example.com"}, format="json").status_code == 200
        refused = api_client.post(RESET_URL, {"token": issued.token, "new_password": NEW_PASSWORD}, format="json")
        assert refused.status_code == 400 and refused.json()["code"] == "reset_token_invalid"
        assert mail.outbox == []  # an account with a password gets no new link; "Forgot password" reaches the new address

    def test_email_taken(self, client, member, admin):
        response = client.patch(detail(member), {"email": "ADMIN@example.com"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "email_taken"

    def test_stale_version(self, client, member):
        response = client.patch(detail(member), {"first_name": "X", "expected_version": member.version + 5}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_cannot_change_own_role(self, client, admin):
        other = RoleFactory(permissions={"dashboard": ["view"]})
        response = client.patch(detail(admin), {"role": str(other.uid)}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "own_role_change"

    def test_can_edit_own_profile(self, client, admin):
        assert client.patch(detail(admin), {"title": "Boss"}, format="json").status_code == 200

    def test_cannot_change_a_user_holding_more_than_you(self, client):
        stronger = UserFactory(role=RoleFactory(permissions={"pricing": ["publish"]}))
        response = client.patch(detail(stronger), {"first_name": "X"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "user_exceeds_own_grants"

    def test_only_a_super_admin_changes_a_super_admin(self, auth_client):
        boss = UserFactory(role=super_admin_role())
        admin = UserFactory(role=seeded_role("admin"))
        response = auth_client(admin).patch(detail(boss), {"first_name": "X"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "super_admin_required"
        other_boss = UserFactory(role=super_admin_role())
        assert auth_client(other_boss).patch(detail(boss), {"first_name": "X"}, format="json").status_code == 200

    def test_validation(self, client, member):
        response = client.patch(detail(member), {"phone_e164": "12", "expected_version": 0}, format="json")
        assert response.status_code == 400 and set(response.json()["errors"]) == {"phone_e164", "expected_version"}


class TestLifecycle:
    def test_deactivate_signs_out_everywhere_and_emits_an_event(self, api_client, client, admin, member):
        issued = sessions.start_session(member)
        response = client.post(detail(member, "deactivate/"), {"note": "left the company", "expected_version": member.version}, format="json")
        assert response.status_code == 200 and response.json()["is_active"] is False
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        assert api_client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {issued.access}").status_code == 401
        entry = AuditLog.objects.get(action="accounts.user_deactivated")
        assert entry.actor_id == admin.pk and entry.note == "left the company"
        event = OutboxEvent.objects.get(event_type="accounts.user_deactivated")
        assert event.payload == {"user_uid": str(member.uid), "reason": "deactivated"} and event.aggregate_uid == member.uid
        assert client.post(detail(member, "deactivate/"), {}, format="json").status_code == 200  # idempotent

    def test_cannot_deactivate_or_delete_yourself(self, client, admin):
        for suffix, method in (("deactivate/", "post"), ("", "delete")):
            response = getattr(client, method)(detail(admin, suffix), {}, format="json")
            assert response.status_code == 403 and response.json()["code"] == "self_action_denied"

    def test_admin_cannot_deactivate_a_super_admin(self, auth_client):
        boss = UserFactory(role=super_admin_role())
        admin = UserFactory(role=seeded_role("admin"))
        assert auth_client(admin).post(detail(boss, "deactivate/"), {}, format="json").json()["code"] == "super_admin_required"

    def test_reactivate(self, client, member):
        User.objects.filter(pk=member.pk).update(is_active=False)
        response = client.post(detail(member, "reactivate/"), {}, format="json")
        assert response.status_code == 200 and response.json()["is_active"] is True
        assert AuditLog.objects.filter(action="accounts.user_reactivated").exists()
        assert OutboxEvent.objects.filter(event_type="accounts.user_reactivated").exists()

    def test_action_stale_version(self, client, member):
        response = client.post(detail(member, "deactivate/"), {"expected_version": 99}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_force_reset(self, client, member, django_capture_on_commit_callbacks):
        issued = sessions.start_session(member)
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(detail(member, "force-reset/"), {"note": "suspected leak"}, format="json")
        assert response.status_code == 200 and response.json()["must_reset_password"] is True
        member.refresh_from_db()
        assert not member.has_usable_password()
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        assert mail.outbox[-1].to == ["member@example.com"] and "#token=" in mail.outbox[-1].body
        assert AuditLog.objects.get(action="accounts.user_force_reset").note == "suspected leak"

    def test_cannot_force_reset_yourself(self, client, admin):
        assert client.post(detail(admin, "force-reset/"), {}, format="json").json()["code"] == "self_action_denied"

    def test_delete_is_a_soft_delete_that_frees_the_email(self, client, member, member_role):
        issued = sessions.start_session(member)
        assert client.delete(detail(member)).status_code == 204
        assert User.all_objects.get(pk=member.pk).deleted_at is not None
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        assert AuditLog.objects.get(action="accounts.user_deleted").before["email"] == "member@example.com"
        assert client.post(URL, {"email": "member@example.com", "role": str(member_role.uid)}, format="json").status_code == 201

    def test_delete_stale_version(self, client, member):
        assert client.delete(f"{detail(member)}?expected_version=42").status_code == 409
