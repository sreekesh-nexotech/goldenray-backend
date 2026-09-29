import pytest

from pricing.services import provider


@pytest.fixture(autouse=True)
def _pricing_price_provider():
    provider.register()
    yield


@pytest.fixture
def admin_user(make_user):
    return make_user(grants={"packs": "*", "engineering": "*", "pricing_internal": ["view"], "catalog": ["view"]})


@pytest.fixture
def admin(auth_client, admin_user):
    return auth_client(admin_user)


@pytest.fixture
def editor(auth_client, make_user):
    """Project Head's packs grants: view, edit, submit (no approve, no publish) and engineering verify."""
    return auth_client(make_user(grants={"packs": ["view", "edit", "submit"], "engineering": ["view", "verify"]}))


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"packs": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    return auth_client(make_user(grants={"catalog": ["view"]}))


@pytest.fixture
def world(db):
    from packs.tests import factories

    return factories.world()
