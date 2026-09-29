"""Inventory test fixtures: the INVENTORY_STOCK flag switched on for every test (``stock_off`` switches it off), the
seeded Procurement grants on inventory, a component and a store."""

from decimal import Decimal

import pytest

from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from core.services.flags import set_flag
from inventory.tests.factories import LocationFactory

PROCUREMENT_GRANTS = {"catalog": ["view", "create", "edit"], "procurement": "*", "pricing_internal": ["view"], "inventory": ["view", "edit"]}


@pytest.fixture(autouse=True)
def stock_on(db):
    set_flag("INVENTORY_STOCK", enabled=True, user=None)


@pytest.fixture
def stock_off(stock_on):
    set_flag("INVENTORY_STOCK", enabled=False, user=None)


@pytest.fixture
def keeper(make_user):
    """PLAN §3.2 Procurement: inventory view/edit, scope all."""
    return make_user(grants=PROCUREMENT_GRANTS)


@pytest.fixture
def client(auth_client, keeper):
    return auth_client(keeper)


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"inventory": ["view"]}))


@pytest.fixture
def outsider(auth_client, make_user):
    """Authenticated, but holds no inventory grant."""
    return auth_client(make_user(grants={"catalog": ["view"], "procurement": ["view"]}))


@pytest.fixture
def component():
    return ComponentFactory(sku="PNL-0001", name="Mono PERC 540 W")


@pytest.fixture
def store():
    return LocationFactory(code="HO-STORE", name="Head office store")


def events(event_type: str) -> list[dict]:
    return [row.payload for row in OutboxEvent.objects.filter(event_type=event_type).order_by("id")]


def dec(value) -> Decimal:
    return Decimal(str(value))
