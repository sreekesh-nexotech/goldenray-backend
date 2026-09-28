import pytest

from core import dashboard

pytestmark = pytest.mark.django_db
URL = "/api/v1/dashboard/"


@pytest.fixture(autouse=True)
def _only_test_counters():
    """Apps register real counters (leads, customers …); these tests look at the registry in isolation."""
    saved = {module: list(fns) for module, fns in dashboard._COUNTERS.items()}
    dashboard._COUNTERS.clear()
    yield
    dashboard._COUNTERS.clear()
    dashboard._COUNTERS.update(saved)


@pytest.fixture
def counters():
    registered = []

    def _register(module, fn):
        dashboard.register(module)(fn)
        registered.append((module, fn))

    yield _register
    for module, fn in registered:
        dashboard.unregister(module, fn)


def test_anonymous_401_and_missing_permission_403(api_client, make_user, auth_client):
    assert api_client.get(URL).status_code == 401
    assert auth_client(make_user(grants={"leads": ["view"]})).get(URL).status_code == 403


def test_only_viewable_modules_are_returned(make_user, auth_client, counters):
    counters("leads", lambda user: {"open": 4, "total": 9})
    counters("catalog", lambda user: {"components": 120})
    user = make_user(grants={"dashboard": ["view"], "leads": ["view"], "catalog": ["edit"]})
    response = auth_client(user).get(URL)
    assert response.status_code == 200
    assert response.json() == {"modules": {"leads": {"open": 4, "total": 9}}}


def test_counters_receive_the_user_and_failures_are_isolated(make_user, auth_client, counters):
    seen = []
    counters("leads", lambda user: seen.append(user) or {"open": 1})
    counters("leads", lambda user: 1 / 0)
    user = make_user(grants={"dashboard": ["view"], "leads": ["view"]})
    assert auth_client(user).get(URL).json() == {"modules": {"leads": {"open": 1}}}
    assert seen == [user]


def test_register_rejects_unknown_modules():
    with pytest.raises(ValueError):
        dashboard.register("nope")
