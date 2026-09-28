"""hr.employee_deactivated handler, deactivate_for_system, retention task, dashboard counters, lockout unit tests."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from accounts.models import LoginAttempt, PasswordReset, User, UserSession
from accounts.services import lockout, passwords, sessions
from accounts.services.users import deactivate_for_system
from accounts.tasks import purge_auth_records
from audit.models import AuditLog
from core.outbox import emit

pytestmark = pytest.mark.django_db


class TestEmployeeDeactivated:
    def test_deactivates_the_linked_user_through_the_outbox(self, make_user, drain_outbox):
        user = make_user()
        issued = sessions.start_session(user)
        employee_uid = uuid.uuid4()
        emit("hr.employee_deactivated", {"employee_uid": str(employee_uid), "user_uid": str(user.uid)}, aggregate_type="hr.employee", aggregate_uid=employee_uid)
        outcome = drain_outbox()
        assert outcome["processed"] >= 1
        user.refresh_from_db()
        assert user.is_active is False
        assert UserSession.objects.get(pk=issued.session.pk).revoked_at is not None
        entry = AuditLog.objects.get(action="accounts.user_deactivated")
        assert entry.actor_kind == "SYSTEM" and entry.actor_id is None and str(employee_uid) in entry.note
        assert entry.after["reason"] == "employee_deactivated"

    def test_employee_without_a_login_is_a_no_op(self, drain_outbox):
        emit("hr.employee_deactivated", {"employee_uid": str(uuid.uuid4()), "user_uid": None})
        assert drain_outbox()["processed"] >= 1
        assert not AuditLog.objects.filter(action="accounts.user_deactivated").exists()

    def test_redelivery_and_unknown_users_are_harmless(self, make_user):
        user = make_user()
        assert deactivate_for_system(user.uid, reason="test") is True
        assert deactivate_for_system(user.uid, reason="test") is False
        assert deactivate_for_system(uuid.uuid4(), reason="test") is False
        assert deactivate_for_system("garbage", reason="test") is False
        assert AuditLog.objects.filter(action="accounts.user_deactivated").count() == 1


def test_purge_task_applies_the_retention_rules(make_user):
    user = make_user()
    with freeze_time("2026-01-01 00:00:00"):
        LoginAttempt.objects.create(email="old@example.com", succeeded=False)
        sessions.start_session(user)  # outstanding refresh token expiring on 2026-01-08
        passwords.issue_reset(user)  # expires 2026-01-01 01:00
    LoginAttempt.objects.create(email="new@example.com", succeeded=False)
    fresh = sessions.start_session(user)
    passwords.issue_reset(user)
    with freeze_time(timezone.now() + timedelta(days=1)):
        result = purge_auth_records()
    assert result["login_attempts"] == 1 and result["tokens"] >= 1 and result["password_resets"] == 1
    assert list(LoginAttempt.objects.values_list("email", flat=True)) == ["new@example.com"]
    assert OutstandingToken.objects.filter(jti=fresh.session.refresh_jti.hex).exists()
    assert PasswordReset.objects.count() == 1


def test_dashboard_counts_users_for_users_viewers(auth_client, make_user):
    viewer = make_user(grants={"dashboard": ["view"], "users": ["view"]})
    User.objects.filter(pk=make_user().pk).update(is_active=False)
    body = auth_client(viewer).get("/api/v1/dashboard/").json()
    assert body["modules"]["users"] == {"active": User.objects.filter(is_active=True).count(), "inactive": 1}
    blind = make_user(grants={"dashboard": ["view"]})
    assert "users" not in auth_client(blind).get("/api/v1/dashboard/").json()["modules"]


class TestLockoutUnit:
    def test_retry_after_counts_from_the_oldest_counted_failure(self):
        with freeze_time("2026-05-01 10:00:00") as frozen:
            for _ in range(5):
                lockout.record_attempt("a@example.com", "198.51.100.1", succeeded=False)
                frozen.tick(timedelta(minutes=1))
            state = lockout.check("A@example.com ", None)
            assert state.locked and state.reason == "email"
            assert 10 * 60 <= state.retry_after <= 11 * 60
            assert not lockout.check("b@example.com", "198.51.100.2").locked
            assert lockout.check("b@example.com", "198.51.100.1").reason == "ip"

    def test_email_key(self):
        assert lockout.email_key("  Mixed@Example.COM ") == "mixed@example.com"
        assert lockout.email_key(None) == ""
        assert len(lockout.email_key("x" * 400)) == 254

    def test_purge_before(self):
        with freeze_time("2026-01-01"):
            lockout.record_attempt("a@example.com", None, succeeded=True)
        lockout.record_attempt("b@example.com", None, succeeded=True)
        assert lockout.purge_before(timezone.now() - timedelta(days=1)) == 1
