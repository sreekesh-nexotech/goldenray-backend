"""auth/password/change/, auth/password/reset-request/, auth/password/reset/ and the password policy."""

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.utils import timezone
from freezegun import freeze_time

from accounts.models import LoginAttempt, PasswordReset, User, UserSession
from accounts.services import passwords, sessions
from accounts.tests.factories import DEFAULT_PASSWORD, NEW_PASSWORD, RoleFactory
from audit.models import AuditLog
from core.errors import DomainError

pytestmark = pytest.mark.django_db
CHANGE = "/api/v1/auth/password/change/"
REQUEST = "/api/v1/auth/password/reset-request/"
RESET = "/api/v1/auth/password/reset/"
ME = "/api/v1/auth/me/"


@pytest.fixture
def user(make_user):
    return make_user(email="meera@example.com", first_name="Meera", last_name="Nair")


def _me(api_client, access):
    return api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")


def _reset_token_from_mail() -> str:
    body = mail.outbox[-1].body
    return body.split("#token=", 1)[1].split()[0]


class TestChange:
    def test_changes_the_password_keeps_this_session_and_ends_the_others(self, api_client, auth_client, user, django_capture_on_commit_callbacks):
        client = auth_client(user)
        other = sessions.start_session(user)
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(CHANGE, {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD}, format="json")
        assert response.status_code == 200 and response.json() == {"other_sessions_ended": 1}
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD) and user.password_changed_at is not None and user.password.startswith("argon2")
        assert client.get(ME).status_code == 200
        assert _me(api_client, other.access).status_code == 401
        assert AuditLog.objects.get(action="accounts.password_changed").after == {"other_sessions_ended": 1}
        assert len(mail.outbox) == 1 and "was just changed" in mail.outbox[0].body and mail.outbox[0].to == ["meera@example.com"]
        assert NEW_PASSWORD not in str(list(AuditLog.objects.values_list("before", "after")))

    def test_wrong_current_password_counts_towards_the_lockout(self, auth_client, user):
        client = auth_client(user)
        response = client.post(CHANGE, {"current_password": "not-it-at-all", "new_password": NEW_PASSWORD}, format="json")
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_current_password" and "current_password" in response.json()["errors"]
        assert LoginAttempt.objects.filter(email="meera@example.com", succeeded=False).count() == 1
        for _ in range(4):
            client.post(CHANGE, {"current_password": "not-it-at-all", "new_password": NEW_PASSWORD}, format="json")
        response = client.post(CHANGE, {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD}, format="json")
        assert response.status_code == 429 and response.json()["code"] == "login_locked"
        user.refresh_from_db()
        assert user.check_password(DEFAULT_PASSWORD)

    @pytest.mark.parametrize(
        "new_password,fragment",
        [("short-1", "too short"), ("1234567890123", "entirely numeric"), ("password1234", "too common"), ("meera@example.com", "too similar")],
    )
    def test_policy_applies(self, auth_client, user, new_password, fragment):
        response = auth_client(user).post(CHANGE, {"current_password": DEFAULT_PASSWORD, "new_password": new_password}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert any(fragment in message for message in response.json()["errors"]["new_password"])

    def test_new_password_must_differ(self, auth_client, user):
        response = auth_client(user).post(CHANGE, {"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD}, format="json")
        assert response.status_code == 400 and "new_password" in response.json()["errors"]

    def test_anonymous_is_401(self, api_client):
        assert api_client.post(CHANGE, {"current_password": "a", "new_password": "b"}, format="json").status_code == 401

    def test_missing_fields(self, auth_client, user):
        response = auth_client(user).post(CHANGE, {}, format="json")
        assert response.status_code == 400 and set(response.json()["errors"]) == {"current_password", "new_password"}


class TestResetRequest:
    def test_sends_a_single_use_link_for_an_active_account(self, api_client, user, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.post(REQUEST, {"email": "MEERA@example.com"}, format="json")
        assert response.status_code == 200
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["meera@example.com"]
        token = _reset_token_from_mail()
        reset = PasswordReset.objects.get()
        assert reset.token_hash == passwords.hash_token(token) and token not in reset.token_hash
        assert reset.expires_at <= timezone.now() + timedelta(hours=1)
        assert AuditLog.objects.get(action="accounts.password_reset_requested").object_uid == user.uid

    @pytest.mark.parametrize("state", ["unknown", "inactive", "deleted"])
    def test_answers_the_same_for_accounts_that_cannot_reset(self, api_client, user, state, django_capture_on_commit_callbacks):
        email = "meera@example.com"
        if state == "unknown":
            email = "ghost@example.com"
        elif state == "inactive":
            User.objects.filter(pk=user.pk).update(is_active=False)
        else:
            user.soft_delete()
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.post(REQUEST, {"email": email}, format="json")
        assert response.status_code == 200
        assert response.json() == {"detail": "If the address belongs to an active account, a reset link has been sent."}
        assert mail.outbox == [] and PasswordReset.objects.count() == 0

    def test_a_new_link_voids_the_previous_one(self, api_client, user, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            api_client.post(REQUEST, {"email": "meera@example.com"}, format="json")
        first = _reset_token_from_mail()
        with django_capture_on_commit_callbacks(execute=True):
            api_client.post(REQUEST, {"email": "meera@example.com"}, format="json")
        assert api_client.post(RESET, {"token": first, "new_password": NEW_PASSWORD}, format="json").json()["code"] == "reset_token_invalid"
        assert api_client.post(RESET, {"token": _reset_token_from_mail(), "new_password": NEW_PASSWORD}, format="json").status_code == 204

    def test_at_most_three_links_per_hour(self, api_client, user, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            for _ in range(5):
                assert api_client.post(REQUEST, {"email": "meera@example.com"}, format="json").status_code == 200
        assert len(mail.outbox) == 3
        assert AuditLog.objects.filter(action="accounts.password_reset_throttled").count() == 2

    def test_invalid_email(self, api_client):
        response = api_client.post(REQUEST, {"email": "nope"}, format="json")
        assert response.status_code == 400 and "email" in response.json()["errors"]

    # Security review: the account lookup, the per-hour count, the token and the audit row ran inside the request only
    # for real accounts, so the response time told registered addresses apart from the rest.
    @pytest.mark.parametrize("state", ["active", "unknown", "inactive"])
    def test_the_request_does_the_same_work_whatever_the_address(self, api_client, user, state):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        if state == "inactive":
            User.objects.filter(pk=user.pk).update(is_active=False)
        email = "nobody@example.com" if state == "unknown" else "meera@example.com"
        with CaptureQueriesContext(connection) as queries:
            response = api_client.post(REQUEST, {"email": email}, format="json")
        assert response.status_code == 200
        assert len(queries.captured_queries) == 0  # the lookup and the e-mail happen in accounts.tasks.send_password_reset

    def test_the_queued_task_never_logs_the_address(self, api_client, user):
        from accounts import tasks

        with mock.patch.object(tasks.send_password_reset, "apply_async") as apply_async:
            with self.captured(execute=True):
                api_client.post(REQUEST, {"email": "meera@example.com"}, format="json")
        assert "meera" not in apply_async.call_args.kwargs["argsrepr"] + apply_async.call_args.kwargs["kwargsrepr"]

    def test_a_broker_outage_still_sends_the_link(self, api_client, user):
        from accounts import tasks

        with mock.patch.object(tasks.send_password_reset, "apply_async", side_effect=ConnectionError("broker down")):
            with self.captured(execute=True):
                assert api_client.post(REQUEST, {"email": "meera@example.com"}, format="json").status_code == 200
        assert len(mail.outbox) == 1 and PasswordReset.objects.count() == 1

    def test_the_audit_row_keeps_the_request_id_and_client_ip(self, api_client, user):
        with self.captured(execute=True):
            response = api_client.post(REQUEST, {"email": "meera@example.com"}, format="json", REMOTE_ADDR="198.51.100.23")
        entry = AuditLog.objects.get(action="accounts.password_reset_requested")
        assert str(entry.ip) == "198.51.100.23" and str(entry.request_id) == response["X-Request-ID"]

    @pytest.fixture(autouse=True)
    def _capture(self, django_capture_on_commit_callbacks):
        self.captured = django_capture_on_commit_callbacks


class TestReset:
    @pytest.fixture
    def token(self, user):
        return passwords.issue_reset(user).token

    def test_sets_the_password_and_ends_every_session(self, api_client, user, token, django_capture_on_commit_callbacks):
        issued = sessions.start_session(user)
        User.objects.filter(pk=user.pk).update(must_reset_password=True)
        with django_capture_on_commit_callbacks(execute=True):
            response = api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json")
        assert response.status_code == 204
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD) and not user.must_reset_password and user.password_changed_at
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        assert PasswordReset.objects.get().used_at is not None
        assert AuditLog.objects.filter(action="accounts.password_reset", object_uid=user.uid).exists()
        assert "was just changed" in mail.outbox[-1].body
        login = api_client.post("/api/v1/auth/login/", {"email": "meera@example.com", "password": NEW_PASSWORD}, format="json")
        assert login.status_code == 200

    def test_token_is_single_use(self, api_client, token):
        assert api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json").status_code == 204
        response = api_client.post(RESET, {"token": token, "new_password": "Yet-Another-Pass-77"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "reset_token_invalid"

    def test_expired_token(self, api_client, user):
        with freeze_time("2026-02-01 09:00:00"):
            token = passwords.issue_reset(user).token
        with freeze_time("2026-02-01 10:00:01"):
            response = api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "reset_token_expired"

    @pytest.mark.parametrize("token", ["not-a-real-token", "x" * 256])
    def test_unknown_token(self, api_client, token):
        assert api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json").json()["code"] == "reset_token_invalid"

    def test_inactive_account(self, api_client, user, token):
        User.objects.filter(pk=user.pk).update(is_active=False)
        assert api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json").json()["code"] == "reset_token_invalid"

    def test_policy_failure_does_not_consume_the_token(self, api_client, token):
        response = api_client.post(RESET, {"token": token, "new_password": "short"}, format="json")
        assert response.status_code == 400 and "new_password" in response.json()["errors"]
        assert PasswordReset.objects.get().used_at is None
        assert api_client.post(RESET, {"token": token, "new_password": NEW_PASSWORD}, format="json").status_code == 204

    def test_missing_fields(self, api_client):
        response = api_client.post(RESET, {}, format="json")
        assert response.status_code == 400 and set(response.json()["errors"]) == {"token", "new_password"}


class TestPolicyEverywhere:
    def test_manager_create_user_enforces_the_policy(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            User.objects.create_user("weak@example.com", "12345", role=RoleFactory())

    def test_validate_new_password_rejects_empty_and_huge(self):
        for value in ("", None, "x" * (passwords.MAX_PASSWORD_LENGTH + 1)):
            with pytest.raises(DomainError) as excinfo:
                passwords.validate_new_password(value)
            assert "new_password" in excinfo.value.errors

    def test_apply_new_password_calls_the_password_changed_hooks(self, user):
        with mock.patch("accounts.services.passwords.password_validation.password_changed") as hook:
            passwords.apply_new_password(user, NEW_PASSWORD, actor=user)
        hook.assert_called_once_with(NEW_PASSWORD, user)

    def test_reset_link_carries_the_token_in_the_fragment(self, settings):
        settings.ACCOUNTS_PASSWORD_RESET_URL = "https://flarize.com/studio/reset-password"
        assert passwords.reset_link("abc") == "https://flarize.com/studio/reset-password#token=abc"
