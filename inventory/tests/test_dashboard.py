"""GET dashboard/ — inventory counters while INVENTORY_STOCK is on; the module is left out while it is off."""

import datetime as dt

import pytest
from django.utils import timezone

from catalog.tests.factories import ComponentFactory
from core import dashboard
from inventory.tests.factories import LocationFactory, MovementFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/dashboard/"


@pytest.fixture
def reader(auth_client, make_user):
    return auth_client(make_user(grants={"dashboard": ["view"], "inventory": ["view"]}))


def test_counters(reader, store, component):
    other = ComponentFactory()
    gone = LocationFactory()
    MovementFactory(component=component, location=store, qty="4")
    MovementFactory(component=component, location=LocationFactory(), qty="1")
    MovementFactory(component=other, location=store, qty="2", direction="OUT", reason="ADJUST", note="count")
    MovementFactory(component=other, location=store, qty="1", at=timezone.now() - dt.timedelta(days=30))
    gone.soft_delete()
    counts = reader.get(URL).json()["modules"]["inventory"]
    assert counts == {"stocked_components": 1, "negative_balances": 1, "locations": 2, "movements_last_7_days": 3}


def test_the_module_is_left_out_while_the_flag_is_off(stock_off, reader):
    assert "inventory" not in reader.get(URL).json()["modules"]


def test_users_without_inventory_view_do_not_see_it(auth_client, make_user):
    assert "inventory" not in auth_client(make_user(grants={"dashboard": ["view"]})).get(URL).json()["modules"]


def test_flagged_and_unflagged_counters_of_one_module(stock_off, reader):
    def plain(user):
        return {"plain": 1}

    dashboard.register("inventory")(plain)
    try:
        assert reader.get(URL).json()["modules"]["inventory"] == {"plain": 1}
    finally:
        dashboard.unregister("inventory", plain)
    assert "inventory" not in reader.get(URL).json()["modules"]
