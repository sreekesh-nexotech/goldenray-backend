import pytest
from django.contrib.auth.models import AnonymousUser

from accounts.models import User
from core import scopes
from core.service_credentials import ServicePrincipal, issue

pytestmark = pytest.mark.django_db


@pytest.fixture
def owned_customers_filter():
    def owned(queryset, user):
        return queryset.filter(pk=user.pk)

    scopes.register("customers", "owned")(owned)
    yield owned
    scopes._FILTERS.pop(("customers", "owned"), None)


def _qs():
    return User.objects.all()


def test_no_user_or_anonymous_sees_nothing():
    assert scopes.apply(_qs(), None, "customers").query.is_empty()
    assert scopes.apply(_qs(), AnonymousUser(), "customers").query.is_empty()


def test_service_principal_sees_nothing():
    credential, _ = issue("AGENT", "pc")
    assert scopes.apply(_qs(), ServicePrincipal(credential), "customers").query.is_empty()


def test_user_without_permission_on_the_module_sees_nothing(make_user):
    user = make_user(grants={"leads": ["view"]}, scopes={"leads": "all", "customers": "all"})
    assert list(scopes.apply(_qs(), user, "customers")) == []


def test_all_scope_is_the_identity(make_user):
    user = make_user(grants={"customers": ["view"]}, scopes={"customers": "all"})
    make_user()
    assert scopes.apply(_qs(), user, "customers").count() == 2


def test_narrow_scope_without_a_registered_filter_fails_closed(make_user):
    user = make_user(grants={"customers": ["view"]}, scopes={"customers": "owned"})
    assert scopes.registered_filter("customers", "owned") is None
    assert list(scopes.apply(_qs(), user, "customers")) == []


def test_missing_scope_defaults_to_the_narrowest_and_uses_its_filter(make_user, owned_customers_filter):
    user = make_user(grants={"customers": ["view"]})
    make_user()
    assert list(scopes.apply(_qs(), user, "customers")) == [user]


def test_registered_filter_never_widens(make_user, owned_customers_filter):
    user = make_user(grants={"customers": ["view"]}, scopes={"customers": "owned"})
    other = make_user(grants={"customers": ["view"]}, scopes={"customers": "owned"})
    assert list(scopes.apply(_qs(), user, "customers")) == [user]
    assert list(scopes.apply(_qs(), other, "customers")) == [other]


def test_unknown_module_fails_closed(make_user):
    user = make_user(grants={"customers": ["view"]}, scopes={"customers": "all"})
    assert list(scopes.apply(_qs(), user, "no_such_module")) == []


def test_register_validates():
    with pytest.raises(ValueError):
        scopes.register("no_such_module", "owned")
    with pytest.raises(ValueError):
        scopes.register("catalog", "owned")
    with pytest.raises(ValueError):
        scopes.register("customers", "office")
    with pytest.raises(ValueError):
        scopes.register("customers", "all")
