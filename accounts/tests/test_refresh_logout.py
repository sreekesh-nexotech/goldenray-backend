"""POST auth/refresh/ and auth/logout/: rotation, replay detection, session re-policing, blacklisting."""

import copy
from datetime import timedelta

import jwt
import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, UserSession
from accounts.services import sessions
from audit.models import AuditLog

pytestmark = pytest.mark.django_db
REFRESH = "/api/v1/auth/refresh/"
LOGOUT = "/api/v1/auth/logout/"
ME = "/api/v1/auth/me/"


@pytest.fixture
def user(make_user):
    return make_user(grants={"dashboard": ["view"]})


@pytest.fixture
def issued(user):
    return sessions.start_session(user, ip="127.0.0.1", user_agent="pytest")


def _refresh(api_client, token, **extra):
    return api_client.post(REFRESH, {"refresh": str(token)}, format="json", **extra)


def _me(api_client, access):
    return api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")


def _jti(token) -> str:
    return jwt.decode(token, options={"verify_signature": False})["jti"]


class TestRotation:
    def test_returns_a_new_pair_for_the_same_session(self, api_client, issued):
        response = _refresh(api_client, issued.refresh, HTTP_USER_AGENT="Studio/2.0")
        assert response.status_code == 200
        body = response.json()
        assert body["session_uid"] == str(issued.session.uid)
        assert body["refresh"] != issued.refresh and body["access"] != issued.access
        session = UserSession.objects.get(pk=issued.session.pk)
        assert session.refresh_jti.hex == _jti(body["refresh"]) and session.user_agent == "Studio/2.0"
        assert BlacklistedToken.objects.filter(token__jti=_jti(issued.refresh)).exists()
        assert _me(api_client, body["access"]).status_code == 200
        assert _me(api_client, issued.access).status_code == 200  # the old access token lives until it expires

    def test_expired_access_token_in_the_header_does_not_block_a_refresh(self, api_client, issued):
        assert _refresh(api_client, issued.refresh, HTTP_AUTHORIZATION="Bearer expired-or-garbage").status_code == 200

    def test_session_slides_but_never_outlives_the_absolute_cap(self, api_client, user):
        with freeze_time("2026-01-01 09:00:00") as frozen:
            issued = sessions.start_session(user)
            token = issued.refresh
            for days in (6, 6, 6, 6, 5):  # refreshed on days 6, 12, 18, 24 and 29
                frozen.tick(timedelta(days=days))
                response = _refresh(api_client, token)
                assert response.status_code == 200, response.json()
                token = response.json()["refresh"]
            session = UserSession.objects.get(pk=issued.session.pk)
            assert session.expires_at == session.created_at + settings.ACCOUNTS_SESSION_MAX_AGE  # capped at day 30
            frozen.tick(timedelta(days=1, seconds=1))
            response = _refresh(api_client, token)
            assert response.status_code == 401 and response.json()["code"] == "session_expired"


class TestReplay:
    def test_replay_after_the_grace_window_ends_the_session(self, api_client, issued):
        with freeze_time(timezone.now()) as frozen:
            new = _refresh(api_client, issued.refresh).json()
            frozen.tick(timedelta(seconds=settings.ACCOUNTS_REFRESH_REUSE_GRACE_SECONDS + 1))
            response = _refresh(api_client, issued.refresh)
            assert response.status_code == 401 and response.json()["code"] == "refresh_token_reused"
            session = UserSession.objects.get(pk=issued.session.pk)
            assert session.revoked_at is not None
            # The legitimate holder of the newest tokens is signed out too.
            assert _refresh(api_client, new["refresh"]).json()["code"] == "session_revoked"
            assert _me(api_client, new["access"]).status_code == 401
        detected = AuditLog.objects.get(action="accounts.refresh_token_reused")
        assert detected.object_uid == issued.session.uid and detected.actor_kind == "SYSTEM"
        assert AuditLog.objects.get(action="accounts.session_ended").after["reason"] == "refresh_token_reused"

    def test_replay_within_the_grace_window_is_refused_without_ending_the_session(self, api_client, issued):
        new = _refresh(api_client, issued.refresh).json()
        response = _refresh(api_client, issued.refresh)
        assert response.status_code == 401 and response.json()["code"] == "refresh_token_rotated"
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is None
        assert _refresh(api_client, new["refresh"]).status_code == 200


class TestPolicing:
    def test_logout_then_refresh_is_rejected(self, api_client, issued):
        assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 204
        response = _refresh(api_client, issued.refresh)
        assert response.status_code == 401 and response.json()["code"] == "session_revoked"

    def test_inactive_user_cannot_refresh_and_the_session_ends(self, api_client, issued, user):
        User.objects.filter(pk=user.pk).update(is_active=False)
        response = _refresh(api_client, issued.refresh)
        assert response.status_code == 401 and response.json()["code"] == "user_inactive"
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        assert AuditLog.objects.get(action="accounts.session_ended").after["reason"] == "user_inactive"

    def test_soft_deleted_user_cannot_refresh(self, api_client, issued, user):
        user.soft_delete()
        assert _refresh(api_client, issued.refresh).json()["code"] == "user_inactive"

    def test_reset_required_ends_the_session(self, api_client, issued, user):
        User.objects.filter(pk=user.pk).update(must_reset_password=True)
        response = _refresh(api_client, issued.refresh)
        assert response.status_code == 403 and response.json()["code"] == "password_reset_required"
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None

    def test_expired_session_is_refused(self, api_client, issued):
        UserSession.objects.filter(pk=issued.session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        assert _refresh(api_client, issued.refresh).json()["code"] == "session_expired"

    def test_blacklisted_current_token_is_refused(self, api_client, issued):
        sessions.blacklist_jtis([issued.session.refresh_jti])
        assert _refresh(api_client, issued.refresh).json()["code"] == "token_invalid"


class TestInvalidTokens:
    @pytest.mark.parametrize("token", ["garbage", "a.b.c", "x" * 5000])
    def test_garbage(self, api_client, token):
        response = _refresh(api_client, token)
        assert response.status_code in (400, 401)
        assert response.json()["code"] in ("token_invalid", "validation_error")

    def test_access_token_is_not_a_refresh_token(self, api_client, issued):
        assert _refresh(api_client, issued.access).json()["code"] == "token_invalid"

    def test_refresh_token_without_a_session_is_refused(self, api_client, user):
        """SimpleJWT's own RefreshToken.for_user() mints no `sid`: it can never be refreshed here."""
        assert _refresh(api_client, RefreshToken.for_user(user)).json()["code"] == "token_invalid"

    def test_token_whose_subject_does_not_own_the_session_is_refused(self, api_client, issued, make_user):
        forged = RefreshToken()
        forged["sub"] = str(make_user().uid)
        forged["sid"] = str(issued.session.uid)
        assert _refresh(api_client, forged).json()["code"] == "token_invalid"

    def test_expired_refresh_token(self, api_client, user):
        with freeze_time("2026-01-01 09:00:00"):
            issued = sessions.start_session(user)
        with freeze_time("2026-01-09 09:00:00"):
            assert _refresh(api_client, issued.refresh).json()["code"] == "token_invalid"

    def test_missing_field(self, api_client):
        response = api_client.post(REFRESH, {}, format="json")
        assert response.status_code == 400 and "refresh" in response.json()["errors"]


class TestLogout:
    def test_revokes_the_session_and_its_access_tokens(self, api_client, issued, user):
        assert _me(api_client, issued.access).status_code == 200
        assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 204
        assert _me(api_client, issued.access).status_code == 401
        assert BlacklistedToken.objects.filter(token__jti=_jti(issued.refresh)).exists()
        entry = AuditLog.objects.get(action="accounts.logout")
        assert entry.actor_id == user.pk and entry.after["reason"] == "logout"

    def test_is_idempotent(self, api_client, issued):
        assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 204
        assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 204
        assert AuditLog.objects.filter(action="accounts.logout").count() == 1

    def test_expired_token_is_a_no_op(self, api_client, user):
        with freeze_time("2026-01-01 09:00:00"):
            issued = sessions.start_session(user)
        with freeze_time("2026-01-09 09:00:00"):
            assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 204

    def test_invalid_token(self, api_client):
        response = api_client.post(LOGOUT, {"refresh": "garbage"}, format="json")
        assert response.status_code == 401 and response.json()["code"] == "token_invalid"

    def test_foreign_session_token_is_refused(self, api_client, issued, make_user):
        forged = RefreshToken()
        forged["sub"] = str(make_user().uid)
        forged["sid"] = str(issued.session.uid)
        assert api_client.post(LOGOUT, {"refresh": str(forged)}, format="json").status_code == 401
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is None


def test_refresh_and_logout_use_the_token_refresh_throttle(api_client, user):
    rest = copy.deepcopy(settings.REST_FRAMEWORK)
    rest["DEFAULT_THROTTLE_RATES"]["token_refresh"] = "1/min"
    with override_settings(REST_FRAMEWORK=rest):
        issued = sessions.start_session(user)
        assert _refresh(api_client, issued.refresh).status_code == 200
        assert _refresh(api_client, issued.refresh).status_code == 429
        assert api_client.post(LOGOUT, {"refresh": issued.refresh}, format="json").status_code == 429
        assert _refresh(api_client, issued.refresh, REMOTE_ADDR="198.51.100.3").status_code == 401  # other IP: not throttled
