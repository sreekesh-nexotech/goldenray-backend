import pytest

from catalog.services import pricing_hooks


@pytest.fixture(autouse=True)
def _default_price_provider():
    pricing_hooks.reset()
    yield
    pricing_hooks.reset()


@pytest.fixture
def catalog_user(make_user):
    return make_user(grants={"catalog": "*"})


@pytest.fixture
def client(auth_client, catalog_user):
    return auth_client(catalog_user)


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"catalog": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    """Authenticated, but holds no catalog grant."""
    return auth_client(make_user(grants={"media": ["view"]}))
