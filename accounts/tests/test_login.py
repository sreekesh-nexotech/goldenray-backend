"""POST auth/login/ and the login service: sessions, lockout, timing normalisation, audit."""

import copy
import threading
import time
from datetime import timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.hashers import Argon2PasswordHasher, PBKDF2PasswordHasher, make_password
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from accounts.errors import InvalidCredentials, LoginLocked, PasswordResetRequired
from accounts.models import LoginAttempt, User, UserSession
from accounts.services import auth, passwords
from accounts.tests.factories import DEFAULT_PASSWORD
from audit.models import AuditLog

pytestmark = pytest.mark.django_db
URL = "/api/v1/auth/login/"


@pytest.fixture
def user(make_user):
    return make_user(grants={"dashboard": ["view"]}, email="asha@example.com")


def _login(api_client, email="asha@example.com", password=DEFAULT_PASSWORD, **extra):
    return api_client.post(URL, {"email": email, "password": password}, format="json", **extra)


def _fail(api_client, times, email="asha@example.com", **extra):
    for _ in range(times):
        assert _login(api_client, email=email, password="wrong-password-123", **extra).status_code == 401


class TestHappyPath:
    def test_returns_a_token_pair_bound_to_a_new_session(self, api_client, user):
        response = _login(api_client, email="ASHA@example.com", HTTP_USER_AGENT="Studio/1.0", REMOTE_ADDR="203.0.113.7")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"access", "refresh", "token_type", "access_expires_at", "refresh_expires_at", "session_uid"}
        assert body["token_type"] == "Bearer"
        session = UserSession.objects.get(uid=body["session_uid"])
        assert session.user == user and session.user_agent == "Studio/1.0" and session.ip == "203.0.113.7"
        assert session.revoked_at is None and session.expires_at > timezone.now() + timedelta(days=6)
        me = api_client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        assert me.status_code == 200 and me.json()["user"]["email"] == "asha@example.com"

    def test_records_the_attempt_the_login_time_and_an_audit_row(self, api_client, user):
        response = _login(api_client, REMOTE_ADDR="203.0.113.7")
        attempt = LoginAttempt.objects.get()
        assert attempt.succeeded and attempt.email == "asha@example.com" and attempt.ip == "203.0.113.7"
        user.refresh_from_db()
        assert user.last_login_at is not None
        entry = AuditLog.objects.get(action="accounts.login")
        assert entry.actor_id == user.pk and entry.actor_kind == "USER" and entry.object_uid == user.uid
        assert entry.after == {"session_uid": response.json()["session_uid"]}
        assert entry.ip == "203.0.113.7" and entry.request_id is not None

    def test_legacy_hash_is_upgraded_to_argon2(self, api_client, user):
        User.objects.filter(pk=user.pk).update(password=make_password(DEFAULT_PASSWORD, hasher=PBKDF2PasswordHasher()))
        assert _login(api_client).status_code == 200
        user.refresh_from_db()
        assert user.password.startswith("argon2")


class TestFailures:
    @pytest.mark.parametrize("email,password", [("asha@example.com", "wrong-password-123"), ("nobody@example.com", DEFAULT_PASSWORD)])
    def test_wrong_password_and_unknown_email_look_identical(self, api_client, user, email, password):
        response = _login(api_client, email=email, password=password)
        assert response.status_code == 401
        assert response.json() == {
            "code": "invalid_credentials",
            "message": "The e-mail address or password is incorrect.",
            "errors": {},
            "error_codes": ["invalid_credentials"],
        }
        attempt = LoginAttempt.objects.get()
        assert not attempt.succeeded and attempt.email == email
        entry = AuditLog.objects.get(action="accounts.login_failed")
        assert entry.actor_id is None and entry.after["email"] == email
        assert UserSession.objects.count() == 0

    def test_inactive_user_gets_invalid_credentials(self, api_client, user):
        user.is_active = False
        user.save()
        assert _login(api_client).json()["code"] == "invalid_credentials"
        assert AuditLog.objects.get(action="accounts.login_failed").after["reason"] == "inactive"

    def test_soft_deleted_user_cannot_log_in(self, api_client, user):
        user.soft_delete()
        assert _login(api_client).json()["code"] == "invalid_credentials"

    def test_account_waiting_for_a_reset_gets_no_tokens(self, api_client, user):
        user.must_reset_password = True
        user.save()
        response = _login(api_client)
        assert response.status_code == 403
        assert response.json()["code"] == "password_reset_required"
        assert UserSession.objects.count() == 0
        assert AuditLog.objects.filter(action="accounts.login_blocked").exists()

    def test_account_without_a_usable_password_cannot_log_in(self, api_client, user):
        user.set_unusable_password()
        user.save()
        assert _login(api_client).json()["code"] == "invalid_credentials"

    @pytest.mark.parametrize(
        "payload,field",
        [({"password": "x"}, "email"), ({"email": "asha@example.com"}, "password"), ({"email": "not-an-email", "password": "x"}, "email"), ({"email": "a@b.co", "password": "x" * 1025}, "password")],
    )
    def test_validation_errors_use_the_envelope(self, api_client, payload, field):
        response = api_client.post(URL, payload, format="json")
        assert response.status_code == 400
        assert response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_authorization_header_is_ignored(self, api_client, user):
        assert _login(api_client, HTTP_AUTHORIZATION="Bearer garbage").status_code == 200


class TestLockout:
    def test_five_failures_lock_the_email_even_with_the_right_password(self, api_client, user):
        _fail(api_client, 5)
        response = _login(api_client)
        assert response.status_code == 429
        assert response.json()["code"] == "login_locked"
        assert 1 <= int(response["Retry-After"]) <= 15 * 60
        assert LoginAttempt.objects.filter(succeeded=False).count() == 5  # the refused attempt is not counted
        assert AuditLog.objects.get(action="accounts.login_locked").after == {"email": "asha@example.com", "locked_by": "email"}

    def test_unknown_emails_are_locked_the_same_way(self, api_client, user):
        _fail(api_client, 5, email="ghost@example.com")
        assert _login(api_client, email="ghost@example.com").status_code == 429

    def test_lock_expires_after_the_window(self, api_client, user):
        with freeze_time("2026-03-01 09:00:00") as frozen:
            _fail(api_client, 5)
            assert _login(api_client).status_code == 429
            frozen.tick(timedelta(minutes=15, seconds=1))
            assert _login(api_client).status_code == 200

    def test_success_clears_the_email_count(self, api_client, user):
        # Separate addresses so only the e-mail counter is exercised (the per-IP counter never resets).
        _fail(api_client, 4, REMOTE_ADDR="198.51.100.1")
        assert _login(api_client, REMOTE_ADDR="198.51.100.2").status_code == 200
        _fail(api_client, 4, REMOTE_ADDR="198.51.100.3")
        assert _login(api_client, REMOTE_ADDR="198.51.100.4").status_code == 200

    def test_per_ip_lockout_spans_emails_and_is_not_reset_by_a_success(self, api_client, user, make_user):
        make_user(email="other@example.com")
        for index in range(4):
            _fail(api_client, 1, email=f"spray{index}@example.com", REMOTE_ADDR="198.51.100.9")
        assert _login(api_client, email="other@example.com", REMOTE_ADDR="198.51.100.9").status_code == 200
        _fail(api_client, 1, email="spray9@example.com", REMOTE_ADDR="198.51.100.9")
        response = _login(api_client, REMOTE_ADDR="198.51.100.9")
        assert response.status_code == 429
        assert AuditLog.objects.filter(action="accounts.login_locked").last().after["locked_by"] == "ip"
        assert _login(api_client, REMOTE_ADDR="198.51.100.10").status_code == 200  # another address is unaffected

    def test_ip_comes_from_the_trusted_proxy_chain_only(self, api_client, user):
        # Spoofed X-Forwarded-For from an untrusted peer is ignored: the peer address is what gets counted.
        _fail(api_client, 5, email="x@example.com", REMOTE_ADDR="198.51.100.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
        assert set(LoginAttempt.objects.values_list("ip", flat=True)) == {"198.51.100.9"}
        LoginAttempt.objects.all().delete()
        _fail(api_client, 1, email="y@example.com", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.50")
        assert LoginAttempt.objects.get().ip == "203.0.113.50"

    @override_settings(ACCOUNTS_LOGIN_MAX_FAILURES_PER_IP=0)
    def test_ip_limit_can_be_disabled(self, api_client, user):
        _fail(api_client, 3, email="x@example.com")
        assert _login(api_client).status_code == 200


class TestTimingNormalisation:
    """Every path runs the password hasher, so response time does not reveal existence or lock state."""

    def _verify_calls(self, fn):
        original = Argon2PasswordHasher.verify
        with mock.patch.object(Argon2PasswordHasher, "verify", autospec=True, side_effect=original) as spy:
            fn()
        return spy.call_count

    def test_unknown_email_runs_the_hasher(self, user):
        def attempt():
            with pytest.raises(InvalidCredentials):
                auth.login("nobody@example.com", DEFAULT_PASSWORD)

        assert self._verify_calls(attempt) == 1

    def test_known_email_runs_the_hasher(self, user):
        def attempt():
            with pytest.raises(InvalidCredentials):
                auth.login("asha@example.com", "wrong-password-123")

        assert self._verify_calls(attempt) == 1

    def test_locked_out_attempt_still_runs_the_hasher(self, user):
        for _ in range(5):
            with pytest.raises(InvalidCredentials):
                auth.login("asha@example.com", "wrong-password-123")

        def attempt():
            with pytest.raises(LoginLocked):
                auth.login("asha@example.com", DEFAULT_PASSWORD)

        assert self._verify_calls(attempt) == 1

    def test_unusable_password_runs_the_hasher(self, user):
        user.set_unusable_password()
        user.save()

        def attempt():
            with pytest.raises(InvalidCredentials):
                auth.login("asha@example.com", DEFAULT_PASSWORD)

        assert self._verify_calls(attempt) == 1

    def test_reset_required_runs_the_hasher(self, user):
        User.objects.filter(pk=user.pk).update(must_reset_password=True)

        def attempt():
            with pytest.raises(PasswordResetRequired):
                auth.login("asha@example.com", DEFAULT_PASSWORD)

        assert self._verify_calls(attempt) == 1


def test_login_endpoint_is_throttled_per_client_ip(api_client, user):
    rest = copy.deepcopy(settings.REST_FRAMEWORK)
    rest["DEFAULT_THROTTLE_RATES"]["login"] = "2/min"
    with override_settings(REST_FRAMEWORK=rest):
        assert _login(api_client, REMOTE_ADDR="198.51.100.1").status_code == 200
        assert _login(api_client, REMOTE_ADDR="198.51.100.1").status_code == 200
        response = _login(api_client, REMOTE_ADDR="198.51.100.1")
        assert response.status_code == 429 and response.json()["code"] == "throttled"
        assert _login(api_client, REMOTE_ADDR="198.51.100.2").status_code == 200


# ── Security review: the lockout was check → verify (slow) → record, so concurrent attempts all passed the check before
# any of them was counted; a burst from many addresses got as many guesses as the server had threads. ─────────────────
def _concurrent(attempt, count: int, *, verify_delay: float = 0.3) -> list[str]:
    """Run ``attempt(index)`` in ``count`` threads released together; each verification takes ``verify_delay``."""
    barrier = threading.Barrier(count)
    outcomes: list[str] = []
    guard = threading.Lock()
    original = passwords.verify_password

    def slow_verify(user, raw_password):
        time.sleep(verify_delay)  # the Argon2 window between the lockout check and the moment the attempt is counted
        return original(user, raw_password)

    def worker(index: int) -> None:
        try:
            barrier.wait()
            try:
                attempt(index)
                outcome = "ok"
            except Exception as exc:  # noqa: BLE001 - the outcome is what the test inspects
                outcome = getattr(exc, "code", type(exc).__name__)
            with guard:
                outcomes.append(outcome)
        finally:
            connection.close()

    # Audit rows are not rolled back by the transactional test teardown (audit_log is unmanaged): keep them out.
    with mock.patch("accounts.services.auth.record"), mock.patch.object(passwords, "verify_password", side_effect=slow_verify):
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_concurrent_guesses_from_many_addresses_cannot_outrun_the_email_lockout(make_user):
    make_user(email="asha@example.com")
    outcomes = _concurrent(lambda index: auth.login("asha@example.com", "wrong-password-123", ip=f"198.51.100.{index + 1}"), 8)
    assert sorted(outcomes) == ["invalid_credentials"] * 5 + ["login_locked"] * 3
    assert LoginAttempt.objects.filter(email="asha@example.com", succeeded=False).count() == 5


@pytest.mark.django_db(transaction=True)
def test_concurrent_guesses_from_one_address_cannot_outrun_the_ip_lockout(make_user):
    outcomes = _concurrent(lambda index: auth.login(f"spray{index}@example.com", "wrong-password-123", ip="198.51.100.77"), 8)
    assert sorted(outcomes) == ["invalid_credentials"] * 5 + ["login_locked"] * 3


@pytest.mark.django_db(transaction=True)
def test_concurrent_wrong_current_passwords_cannot_outrun_the_lockout(make_user):
    user = make_user(email="asha@example.com")
    outcomes = _concurrent(lambda index: auth.change_password(user, current_password="not-it-at-all", new_password="Brand-New-Passphrase-77", ip=f"198.51.100.{index + 1}"), 8)
    assert sorted(outcomes) == ["invalid_current_password"] * 5 + ["login_locked"] * 3
