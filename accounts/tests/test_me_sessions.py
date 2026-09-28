"""GET auth/me/, GET auth/sessions/, DELETE auth/sessions/<uid>/."""

import uuid

import pytest

from accounts.models import UserSession
from accounts.services import sessions
from audit.models import AuditLog

pytestmark = pytest.mark.django_db
ME = "/api/v1/auth/me/"
SESSIONS = "/api/v1/auth/sessions/"


class TestMe:
    def test_returns_user_role_permissions_and_scopes(self, auth_client, make_user):
        user = make_user(grants={"customers": ["view", "edit"], "catalog": ["view"]}, scopes={"customers": "owned"}, first_name="Ravi", last_name="K", title="CRS")
        client = auth_client(user)
        response = client.get(ME)
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["uid"] == str(user.uid) and body["user"]["full_name"] == "Ravi K" and body["user"]["title"] == "CRS"
        assert body["role"] == {"uid": str(user.role.uid), "slug": user.role.slug, "name": user.role.name, "is_system": False}
        assert body["permissions"] == {"catalog": ["view"], "customers": ["edit", "view"]}
        assert body["scopes"] == {"catalog": "all", "customers": "owned"}
        assert body["session_uid"] == str(client.tokens.session.uid)
        assert "password" not in body["user"] and "argon2" not in str(body)

    def test_needs_no_module_permission(self, auth_client, make_user):
        assert auth_client(make_user()).get(ME).json()["permissions"] == {}

    def test_anonymous_is_401(self, api_client):
        assert api_client.get(ME).status_code == 401

    def test_role_change_is_visible_on_the_next_request(self, auth_client, make_user):
        from accounts.services.roles import update_role

        admin = make_user(grants={"roles": "*", "catalog": "*"})
        user = make_user(grants={"catalog": ["view"]})
        client = auth_client(user)
        assert client.get(ME).json()["permissions"] == {"catalog": ["view"]}
        update_role(user.role, user=admin, data={"permissions": {"catalog": ["view", "edit"]}})
        assert client.get(ME).json()["permissions"] == {"catalog": ["edit", "view"]}


class TestSessions:
    def test_lists_only_my_live_sessions_and_flags_the_current_one(self, auth_client, make_user):
        user = make_user()
        client = auth_client(user)
        other_device = sessions.start_session(user, ip="198.51.100.4", user_agent="Phone")
        ended = sessions.start_session(user)
        sessions.revoke_session(ended.session, user=user, reason="test")
        sessions.start_session(make_user())
        response = client.get(SESSIONS)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        rows = {row["uid"]: row for row in body["results"]}
        assert set(rows) == {str(client.tokens.session.uid), str(other_device.session.uid)}
        assert rows[str(client.tokens.session.uid)]["current"] is True
        assert rows[str(other_device.session.uid)] | {"current": False} == rows[str(other_device.session.uid)]
        assert rows[str(other_device.session.uid)]["ip"] == "198.51.100.4" and rows[str(other_device.session.uid)]["user_agent"] == "Phone"
        assert set(rows[str(other_device.session.uid)]) == {"uid", "created_at", "last_refreshed_at", "expires_at", "ip", "user_agent", "current"}

    def test_revoke_one_of_my_sessions(self, api_client, auth_client, make_user):
        user = make_user()
        client = auth_client(user)
        other = sessions.start_session(user)
        assert client.delete(f"{SESSIONS}{other.session.uid}/").status_code == 204
        assert UserSession.objects.get(pk=other.session.pk).revoked_at is not None
        assert api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {other.access}").status_code == 401
        assert client.get(ME).status_code == 200
        entry = AuditLog.objects.get(action="accounts.session_revoked")
        assert entry.actor_id == user.pk and entry.object_uid == other.session.uid

    def test_revoking_the_current_session_signs_out(self, auth_client, make_user):
        client = auth_client(make_user())
        assert client.delete(f"{SESSIONS}{client.tokens.session.uid}/").status_code == 204
        assert client.get(ME).status_code == 401

    def test_cannot_touch_someone_elses_session(self, auth_client, make_user):
        client = auth_client(make_user())
        victim = sessions.start_session(make_user())
        response = client.delete(f"{SESSIONS}{victim.session.uid}/")
        assert response.status_code == 404 and response.json()["code"] == "session_not_found"
        assert UserSession.objects.get(pk=victim.session.pk).revoked_at is None

    def test_unknown_or_malformed_uid(self, auth_client, make_user):
        client = auth_client(make_user())
        assert client.delete(f"{SESSIONS}{uuid.uuid4()}/").status_code == 404
        assert client.delete(f"{SESSIONS}not-a-uuid/").status_code == 404

    def test_anonymous_is_401(self, api_client):
        assert api_client.get(SESSIONS).status_code == 401
        assert api_client.delete(f"{SESSIONS}{uuid.uuid4()}/").status_code == 401
