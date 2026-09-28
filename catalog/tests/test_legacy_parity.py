"""Parity evidence: the legacy /api/solar-panels/, /api/solar-inverters/ and /api/batteries/ responses (captured from the
UAT legacy server over the same seeded data) are rebuilt exactly from the catalog tables after the imports, in any
import order."""

from decimal import Decimal

import pytest

from catalog.tests.legacy_fixtures import import_all, legacy_response
from catalog.tests.legacy_rebuild import batteries_response, inverter_row, inverters_response, panel_row, panels_response

pytestmark = pytest.mark.django_db
ORDERS = [("website",), ("flarize", "bom", "website"), ("website", "bom", "flarize")]


def _by_id(rows):
    return {row["id"]: row for row in rows}


def _battery_prices(results) -> dict:
    return {price["sku"]: Decimal(price["amount"]) for price in results["website"]["prices"] if price["source_table"] == "batteries"}


@pytest.mark.parametrize("order", ORDERS, ids=["-".join(order) for order in ORDERS])
def test_legacy_list_responses_rebuild_exactly(order):
    results = import_all(order=order)
    for endpoint, rebuilt in (("solar-panels", panels_response()), ("solar-inverters", inverters_response())):
        legacy = legacy_response(endpoint)
        assert rebuilt["meta"] == legacy["meta"]
        assert _by_id(rebuilt["data"]) == _by_id(legacy["data"]), endpoint
        # Legacy order: -kerala_climate_score (ties are heap order there, legacy id here).
        assert [row["kerala_climate_score"] for row in rebuilt["data"]] == [row["kerala_climate_score"] for row in legacy["data"]]
    assert batteries_response(_battery_prices(results)) == legacy_response("batteries")


def test_detail_rows_match_the_list_rows():
    import_all(order=("website",))
    from catalog.tests.legacy_rebuild import _components

    for legacy_id, component in _components("solar_panels"):
        assert {"data": panel_row(legacy_id, component)}["data"] == _by_id(legacy_response("solar-panels")["data"])[legacy_id]
    for legacy_id, component in _components("solar_inverters"):
        assert inverter_row(legacy_id, component) == _by_id(legacy_response("solar-inverters")["data"])[legacy_id]


def test_every_legacy_column_has_a_home():
    """Every column of the legacy website models appears in the rebuilt rows (nothing was dropped)."""
    from catalog.tests.legacy_fixtures import goldenapp

    import_all(order=("website",))
    for table, rebuilt in (("solar_panels", panels_response()["data"]), ("solar_inverters", inverters_response()["data"])):
        assert set(goldenapp(table)[0]) == set(rebuilt[0])
    assert set(goldenapp("batteries")[0]) - {"created_at", "updated_at"} == set(batteries_response({})[0])
