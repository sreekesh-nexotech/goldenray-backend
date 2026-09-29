import pytest

from catalog.services import pricing_hooks
from pricing.services import provider


@pytest.fixture(autouse=True)
def _pricing_price_provider():
    """Catalog tests reset the price provider; pricing tests run with the real one installed."""
    provider.register()
    yield
    pricing_hooks.reset()


@pytest.fixture
def pricing_user(make_user):
    return make_user(grants={"pricing": "*", "pricing_internal": ["view"], "market_rates": "*", "offers": "*", "catalog": ["view"]})


@pytest.fixture
def client(auth_client, pricing_user):
    return auth_client(pricing_user)


@pytest.fixture
def editor(auth_client, make_user):
    """pricing view/edit without pricing_internal (no landed costs, no margins)."""
    return auth_client(make_user(grants={"pricing": ["view", "edit"], "market_rates": ["view", "edit"], "offers": ["view", "create", "edit"]}))


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"pricing": ["view"], "market_rates": ["view"], "offers": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    return auth_client(make_user(grants={"catalog": ["view"]}))
