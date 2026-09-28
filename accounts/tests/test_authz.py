import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.services.authz import EMPTY_GRANTS, forget_grants, get_grants, get_scope, has_permission

pytestmark = pytest.mark.django_db


def test_grants_come_from_the_role(make_user):
    user = make_user(grants={"catalog": ["view", "edit"], "customers": ["view"]}, scopes={"customers": "all"})
    grants = get_grants(user)
    assert grants.permissions == {"catalog": frozenset({"view", "edit"}), "customers": frozenset({"view"})}
    assert grants.scopes == {"catalog": "all", "customers": "all"}
    assert has_permission(user, "catalog", "edit") and not has_permission(user, "catalog", "archive")
    assert get_scope(user, "customers") == "all"
    assert get_scope(user, "leads") is None
    assert grants.as_dict() == {"permissions": {"catalog": ["edit", "view"], "customers": ["view"]}, "scopes": {"catalog": "all", "customers": "all"}}


def test_grants_are_memoised_per_user_object(make_user):
    user = make_user(grants={"catalog": ["view"]})
    user = type(user).objects.get(pk=user.pk)
    get_grants(user)
    with CaptureQueriesContext(connection) as queries:
        get_grants(user)
    assert len(queries) == 0
    forget_grants(user)
    forget_grants(user)


def test_grants_are_resolved_from_the_database_not_cached_across_objects(make_user):
    user = make_user(grants={"catalog": ["view"]})
    role = user.role
    role.permissions = {"catalog": ["view", "edit"]}
    role.save()
    fresh = type(user).objects.get(pk=user.pk)
    assert has_permission(fresh, "catalog", "edit")


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
