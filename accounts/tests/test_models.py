import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import authenticate
from django.db import DataError, IntegrityError
from django.utils import timezone

from accounts.models import LoginAttempt, PasswordReset, Role, User, UserSession
from accounts.tests.factories import DEFAULT_PASSWORD, RoleFactory, UserFactory

pytestmark = pytest.mark.django_db


class TestUserManager:
    def test_create_user(self):
        role = RoleFactory()
        user = User.objects.create_user("Asha@Example.COM", "Long-enough-pass-1", role=role, first_name="Asha")
        assert user.email == "Asha@example.com"
        assert user.check_password("Long-enough-pass-1")
        assert user.password.startswith("argon2")
        assert user.is_active and user.is_staff and not user.must_reset_password
        assert user.version == 1 and user.uid

    def test_without_password_is_unusable(self):
        assert not User.objects.create_user("a@example.com", role=RoleFactory()).has_usable_password()

    def test_requires_email_and_role(self):
        with pytest.raises(ValueError):
            User.objects.create_user("", "x", role=RoleFactory())
        with pytest.raises(ValueError):
            User.objects.create_user("a@example.com", "x", role=None)

    def test_there_is_no_superuser(self):
        assert not hasattr(User.objects, "create_superuser")
        assert not hasattr(User, "is_superuser")
        assert "last_login" not in {field.name for field in User._meta.get_fields()}


class TestEmail:
    def test_unique_case_insensitively_among_live_users(self):
        UserFactory(email="Ravi@Example.com")
        with pytest.raises(IntegrityError):
            UserFactory(email="ravi@example.COM")

    def test_soft_deleted_user_releases_the_email(self):
        user = UserFactory(email="ravi@example.com")
        user.soft_delete()
        assert UserFactory(email="RAVI@example.com").pk != user.pk

    def test_lookup_is_case_insensitive_and_live_only(self):
        user = UserFactory(email="meera@example.com")
        assert User.objects.get_by_natural_key("MEERA@EXAMPLE.COM") == user
        user.soft_delete()
        with pytest.raises(User.DoesNotExist):
            User.objects.get_by_natural_key("meera@example.com")
        assert User.all_objects.filter(email="MEERA@example.com").exists()


@pytest.mark.parametrize("phone", ["+919876543210", "+14155550100", ""])
def test_valid_phone_numbers(phone):
    assert UserFactory(phone_e164=phone).phone_e164 == phone


@pytest.mark.parametrize("phone", ["9876543210", "+0123456789", "+91 98765", "+9198765432101234"])
def test_invalid_phone_numbers_are_rejected_by_the_database(phone):
    with pytest.raises((IntegrityError, DataError)):
        UserFactory(phone_e164=phone)


class TestRole:
    def test_grants_are_normalised_on_save(self):
        role = Role.objects.create(slug="sales", name="Sales", permissions={"customers": ["edit", "view", "fly"], "nope": ["view"]}, scopes={"customers": "planet", "leads": "all"})
        role.refresh_from_db()
        assert role.permissions == {"customers": ["view", "edit"]}
        assert role.scopes == {"customers": "owned"}

    def test_versioned_update_normalises_too(self):
        role = RoleFactory()
        role.versioned_update(None, permissions={"audit": ["view", "delete"]})
        role.refresh_from_db()
        assert role.permissions == {"audit": ["view"]} and role.scopes == {"audit": "all"} and role.version == 2

    def test_update_fields_save_keeps_scopes_in_sync(self):
        role = RoleFactory(permissions={"customers": ["view"]}, scopes={"customers": "all"})
        role.permissions = {"leads": ["view"]}
        role.save(update_fields=["permissions"])
        role.refresh_from_db()
        assert role.scopes == {"leads": "owned"}

    def test_slug_unique_among_live_roles_and_legacy_role_checked(self):
        RoleFactory(slug="admin")
        with pytest.raises(IntegrityError):
            RoleFactory(slug="admin")

    def test_legacy_role_values(self):
        assert RoleFactory(legacy_role="editor").legacy_role == "editor"
        with pytest.raises(IntegrityError):
            RoleFactory(legacy_role="owner")

    def test_role_in_use_cannot_be_hard_deleted(self):
        from django.db.models import ProtectedError

        user = UserFactory()
        with pytest.raises(ProtectedError):
            user.role.delete()


def test_session_password_reset_and_login_attempt():
    user = UserFactory()
    jti = uuid.uuid4()
    session = UserSession.objects.create(user=user, refresh_jti=jti, expires_at=timezone.now() + timedelta(days=7), ip="203.0.113.5", user_agent="pytest")
    assert user.sessions.get() == session
    with pytest.raises(IntegrityError):
        UserSession.objects.create(user=user, refresh_jti=jti, expires_at=timezone.now())


def test_password_reset_token_hash_is_unique():
    user = UserFactory()
    PasswordReset.objects.create(user=user, token_hash="a" * 64, expires_at=timezone.now() + timedelta(hours=1))
    with pytest.raises(IntegrityError):
        PasswordReset.objects.create(user=user, token_hash="a" * 64, expires_at=timezone.now())


def test_login_attempts_are_plain_rows():
    attempt = LoginAttempt.objects.create(email="x@example.com", ip="2001:db8::1", succeeded=False)
    assert attempt.at is not None and not hasattr(attempt, "uid")


def test_deleting_a_user_cascades_to_sessions_and_resets():
    user = UserFactory()
    UserSession.objects.create(user=user, refresh_jti=uuid.uuid4(), expires_at=timezone.now())
    PasswordReset.objects.create(user=user, token_hash="b" * 64, expires_at=timezone.now())
    User.all_objects.filter(pk=user.pk).delete()
    assert UserSession.objects.count() == 0 and PasswordReset.objects.count() == 0


class TestEmailBackend:
    def test_authenticates_live_active_users_case_insensitively(self):
        user = UserFactory(email="devi@example.com")
        assert authenticate(username="DEVI@example.com", password=DEFAULT_PASSWORD) == user
        assert authenticate(username="devi@example.com", password="wrong") is None

    def test_inactive_and_deleted_users_cannot_authenticate(self):
        UserFactory(email="inactive@example.com", is_active=False)
        deleted = UserFactory(email="deleted@example.com")
        deleted.soft_delete()
        assert authenticate(username="inactive@example.com", password=DEFAULT_PASSWORD) is None
        assert authenticate(username="deleted@example.com", password=DEFAULT_PASSWORD) is None

    def test_django_permission_api_grants_nothing(self):
        from accounts.backends import EmailBackend

        user = UserFactory()
        backend = EmailBackend()
        assert backend.has_perm(user, "anything") is False
        assert backend.has_module_perms(user, "core") is False
        assert backend.get_all_permissions(user) == set()
        assert backend.get_user_permissions(user) == set() and backend.get_group_permissions(user) == set()
        assert list(backend.with_perm("anything")) == []


def test_bcrypt_hashes_from_essl_verify_and_upgrade_to_argon2():
    from django.contrib.auth.hashers import make_password

    user = UserFactory(password=make_password("essl-password-1", hasher="bcrypt"))
    assert user.password.startswith("bcrypt$")
    assert user.check_password("essl-password-1")
    user.refresh_from_db()
    assert user.password.startswith("argon2")
