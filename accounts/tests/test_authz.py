import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.services import authz
from accounts.services.authz import (
    EMPTY_GRANTS,
    Grants,
    can,
    deny_self_action,
    forget_grants,
    get_grants,
    get_scope,
    has_permission,
    is_self,
    is_super_admin,
    missing_grants,
    scope_covers,
    scope_for,
)
from accounts.tests.factories import UserFactory, seeded_role, super_admin_role
from core.errors import PermissionDenied

pytestmark = pytest.mark.django_db


def test_grants_come_from_the_role(make_user):
    user = make_user(grants={"catalog": ["view", "edit"], "customers": ["view"]}, scopes={"customers": "all"})
    grants = get_grants(user)
    assert grants.permissions == {"catalog": frozenset({"view", "edit"}), "customers": frozenset({"view"})}
    assert grants.scopes == {"catalog": "all", "customers": "all"}
    assert can(user, "catalog", "edit") and not can(user, "catalog", "archive")
    assert has_permission is can and get_scope is scope_for
    assert scope_for(user, "customers") == "all"
    assert scope_for(user, "leads") is None
    assert grants.as_dict() == {"permissions": {"catalog": ["edit", "view"], "customers": ["view"]}, "scopes": {"catalog": "all", "customers": "all"}}
    assert Grants.from_dict(grants.as_dict()) == grants


def test_grants_are_memoised_per_user_object(make_user):
    user = make_user(grants={"catalog": ["view"]})
    user = type(user).objects.get(pk=user.pk)
    get_grants(user)
    with CaptureQueriesContext(connection) as queries:
        get_grants(user)
    assert len(queries) == 0
    forget_grants(user)
    forget_grants(user)


def test_grants_are_cached_across_requests(make_user):
    user = make_user(grants={"catalog": ["view"]})
    get_grants(type(user).objects.select_related("role").get(pk=user.pk))
    fresh = type(user).objects.select_related("role").get(pk=user.pk)
    with mock.patch.object(authz, "grants_for_role", wraps=authz.grants_for_role) as compute:
        assert can(fresh, "catalog", "view")
    compute.assert_not_called()


def test_role_writes_outside_the_services_still_invalidate(make_user):
    user = make_user(grants={"catalog": ["view"]})
    get_grants(user)
    role = user.role
    role.permissions = {"catalog": ["view", "edit"]}
    role.save()
    fresh = type(user).objects.get(pk=user.pk)
    assert has_permission(fresh, "catalog", "edit")


def test_namespace_bump_invalidates_the_cache(make_user):
    user = make_user(grants={"catalog": ["view"]})
    get_grants(user)
    type(user.role).objects.filter(pk=user.role.pk).update(permissions={"catalog": ["view", "archive"]})  # no version/updated_at change
    stale = type(user).objects.select_related("role").get(pk=user.pk)
    stale.role.updated_at, stale.role.version = user.role.updated_at, user.role.version
    assert not can(stale, "catalog", "archive")  # served from the cache
    authz.invalidate_role(user.role.uid)
    fresh = type(user).objects.select_related("role").get(pk=user.pk)
    fresh.role.updated_at, fresh.role.version = user.role.updated_at, user.role.version
    assert can(fresh, "catalog", "archive")


def test_cache_outage_falls_back_to_the_row(make_user):
    user = make_user(grants={"catalog": ["view"]})
    with mock.patch("accounts.services.authz.cache.get", side_effect=ConnectionError), mock.patch("accounts.services.authz.cache.set", side_effect=ConnectionError):
        assert can(user, "catalog", "view")


@pytest.mark.parametrize("state", ["inactive", "deleted", "role_deleted"])
def test_inactive_deleted_or_roleless_users_have_nothing(make_user, state):
    user = make_user(grants={"catalog": ["view"]})
    if state == "inactive":
        user.is_active = False
    elif state == "deleted":
        user.soft_delete()
    else:
        user.role.soft_delete()
    assert get_grants(user) is EMPTY_GRANTS


def test_anonymous_and_none(make_user):
    assert get_grants(AnonymousUser()) is EMPTY_GRANTS
    assert get_grants(None) is EMPTY_GRANTS


class TestEscalationHelpers:
    def test_scope_covers(self):
        assert scope_covers("all", "owned") and scope_covers("owned", "owned") and scope_covers("owned", None)
        assert not scope_covers("owned", "all") and not scope_covers("owned", "assigned") and not scope_covers(None, "all")

    def test_missing_grants(self):
        holder = Grants({"customers": frozenset({"view", "edit"}), "catalog": frozenset({"view"})}, {"customers": "owned", "catalog": "all"})
        assert missing_grants(holder, {"customers": ["view"], "catalog": ["view"]}, {"customers": "owned"}) == {}
        problems = missing_grants(holder, {"customers": ["view", "archive"], "pricing": ["view"]}, {"customers": "all"})
        assert set(problems) == {"customers", "pricing"}
        assert any("archive" in text for text in problems["customers"]) and any("wider" in text for text in problems["customers"])

    def test_super_admin_identity(self, make_user):
        boss = UserFactory(role=super_admin_role())
        admin = UserFactory(role=seeded_role("admin"))
        impostor = make_user(grants={"catalog": ["view"]})
        impostor.role.slug = "super-admin"  # a non-system role can never be the Super Admin role
        assert is_super_admin(boss) and not is_super_admin(admin) and not is_super_admin(impostor) and not is_super_admin(None)
        boss.is_active = False
        assert not is_super_admin(boss)


class TestDenySelfAction:
    def test_targets(self, make_user):
        user, other = make_user(), make_user()
        assert is_self(user, user) and is_self(user, user.uid) and is_self(user, str(user.uid))
        assert is_self(user, SimpleNamespace(user_id=user.pk)) and is_self(user, SimpleNamespace(owner_id=user.pk))
        assert is_self(user, SimpleNamespace(employee=None, user=user))
        assert is_self(user, SimpleNamespace(user=SimpleNamespace(user=user)))  # e.g. attendance day → employee → user
        assert not is_self(user, other) and not is_self(user, other.uid) and not is_self(user, "not-a-uuid")
        assert not is_self(user, None) and not is_self(user, SimpleNamespace()) and not is_self(None, user)
        assert not is_self(user, uuid.uuid4())

    def test_registered_pairs_are_denied_on_your_own_record(self, make_user):
        user, other = make_user(), make_user()
        for module, action in (("attendance", "edit"), ("leave", "approve")):
            with pytest.raises(PermissionDenied) as excinfo:
                deny_self_action(user, SimpleNamespace(user=user), module=module, action=action)
            assert excinfo.value.code == "self_action_denied"
            deny_self_action(user, SimpleNamespace(user=other), module=module, action=action)

    # Security review: records were resolved through `user`/`owner` only, so the documented "attendance day → employee
    # → user" shape (an `employee` FK, as the HR models have) resolved to nobody and the guard silently let a user
    # edit their own attendance or approve their own leave; anything unrecognised was treated as "not yours".
    def test_records_are_followed_through_their_employee(self, make_user):
        user, other = make_user(), make_user()
        day = SimpleNamespace(employee_id=41, employee=SimpleNamespace(user_id=user.pk, user=user))
        leave = SimpleNamespace(employee=SimpleNamespace(user=user))
        assert is_self(user, day) and is_self(user, leave)
        for target, (module, action) in ((day, ("attendance", "edit")), (leave, ("leave", "approve"))):
            with pytest.raises(PermissionDenied) as excinfo:
                deny_self_action(user, target, module=module, action=action)
            assert excinfo.value.code == "self_action_denied"
            deny_self_action(other, target, module=module, action=action)

    def test_an_employee_without_a_login_is_nobody_s_own_record(self, make_user):
        unlinked = SimpleNamespace(employee_id=7, employee=SimpleNamespace(user_id=None, user=None))
        deny_self_action(make_user(), unlinked, module="attendance", action="edit")

    @pytest.mark.parametrize("target", [None, "not-a-uuid", SimpleNamespace(), SimpleNamespace(employee_id=7), SimpleNamespace(status="PENDING")])
    def test_a_record_it_cannot_attribute_fails_closed(self, make_user, target):
        with pytest.raises(ValueError):
            deny_self_action(make_user(), target, module="leave", action="approve")

    def test_misuse_is_a_programming_error(self, make_user):
        user = make_user()
        with pytest.raises(ValueError):
            deny_self_action(user, user, module="catalog", action="edit")
        with pytest.raises(ValueError):
            deny_self_action(user, user, module="attendance")

    def test_without_a_pair_the_guard_always_applies(self, make_user):
        user = make_user()
        with pytest.raises(PermissionDenied, match="nope"):
            deny_self_action(user, user, message="nope")
