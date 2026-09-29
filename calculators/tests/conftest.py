"""Fixtures of the calculators tests: the UAT legacy tables imported through the owners' importers."""

import pytest

from calculators.tests.parity.support import battery_price_provider, import_legacy_rows, load_rows
from catalog.services import pricing_hooks


@pytest.fixture
def legacy_tables(db):
    """The UAT rows (tariffs, devices, EVs, pincodes, sizing tables, batteries with their prices)."""
    previous = pricing_hooks.current_provider()
    prices = import_legacy_rows(load_rows("uat"))
    pricing_hooks.register(battery_price_provider(prices))
    yield prices
    pricing_hooks.register(previous)
