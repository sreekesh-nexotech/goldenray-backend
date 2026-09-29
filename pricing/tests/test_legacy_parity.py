"""Parity evidence: every column of the legacy ``bom`` pricing models survives the import (lossless home).

The UAT main-backend rows (committed fixtures) are imported alone — as the website BOM quote engine will read them —
and rebuilt from the platform tables (:mod:`pricing.tests.legacy_rebuild`); every row must equal its source column
by column: ``bom_globalcosts``, ``bom_marketrate`` (``size_rates`` with its size keys incl. ``5sp``/``5tp`` and key
order, ``bat_config``, upgrade ``from_size``/``to_size``), ``bom_offer`` (``applies_to``/``applies_to_tier``, dates,
``active``) and ``bom_catalogitem.price``/``per_watt``. The legacy ``BomCalculator`` lookups
(``_lookup_market_rate``, ``_lookup_upgrade_market_rate``, the offer filter) are then replayed on both and must agree for
every system/tier/battery/size and upgrade path of the UAT data.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils.dateparse import parse_datetime

from catalog.tests import legacy_fixtures as catalog_fixtures
from pricing.services import legacy_import
from pricing.tests import legacy_rebuild
from pricing.tests.legacy_fixtures import goldenapp

pytestmark = pytest.mark.django_db


def _decimal_rates(rates: dict) -> dict:
    return {key: Decimal(str(value)) for key, value in rates.items()}


def _lookup_market_rate(rows, sys_type, tier, bat_config, size):
    """``BomCalculator._lookup_market_rate`` verbatim (first matching row, 0 when unset)."""
    for mr in rows:
        if mr["system_type"] != sys_type:
            continue
        if sys_type == "ongrid":
            if mr["tier"] == tier and not mr["bat_config"] and not mr["from_size"]:
                value = (mr["size_rates"] or {}).get(size, 0)
                return float(value) if value else 0
        elif sys_type == "hybrid":
            if mr["tier"] == tier and mr["bat_config"] == bat_config:
                value = (mr["size_rates"] or {}).get(size, 0)
                return float(value) if value else 0
    return 0


def _lookup_upgrade(rows, from_kw, to_kw):
    for mr in rows:
        if mr["system_type"] == "upgrade" and mr["from_size"] == str(int(round(from_kw))) and mr["to_size"] == str(int(round(to_kw))):
            value = (mr["size_rates"] or {}).get("rate", 0)
            return float(value) if value else 0
    return 0


def _offers_for(rows, sys_type, tier, today):
    """The view's ``active`` + date filter, then ``BomCalculator._applicable_offers``."""
    result = []
    for o in rows:
        if not o["active"] or (o["start_date"] and o["start_date"] > today) or (o["end_date"] and o["end_date"] < today):
            continue
        if o["applies_to"] not in ("all", sys_type) or o["applies_to_tier"] not in ("all", tier):
            continue
        result.append({"offer_id": o["offer_id"], "name": o["name"], "offer_type": o["offer_type"], "value": float(o["value"])})
    return result


def _as_date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


@pytest.fixture
def imported():
    legacy_import.import_bom_global_costs(goldenapp("bom_globalcosts"))
    legacy_import.import_bom_market_rates(goldenapp("bom_marketrate"))
    legacy_import.import_bom_offers(goldenapp("bom_offer"))


def test_global_costs_rebuild_exactly(imported):
    (source,) = goldenapp("bom_globalcosts")
    rebuilt = legacy_rebuild.global_costs()
    for column in source:
        if column == "updated_at":
            assert rebuilt[column] == parse_datetime(source[column])
        else:
            assert rebuilt[column] == source[column], column


def test_market_rates_rebuild_exactly(imported):
    source = goldenapp("bom_marketrate")
    rebuilt = legacy_rebuild.market_rates()
    assert len(rebuilt) == len(source)
    for original, row in zip(source, rebuilt, strict=True):
        assert list(row["size_rates"]) == list(original["size_rates"]), original["id"]  # key order kept
        assert _decimal_rates(row["size_rates"]) == _decimal_rates(original["size_rates"])
        for column in ("id", "system_type", "tier", "bat_config", "from_size", "to_size"):
            assert row[column] == original[column], (original["id"], column)
        assert row["updated_at"] == parse_datetime(original["updated_at"])


def test_offers_rebuild_exactly(imported):
    source = goldenapp("bom_offer")
    rebuilt = legacy_rebuild.offers()
    for original, row in zip(source, rebuilt, strict=True):
        for column in ("id", "offer_id", "name", "offer_type", "applies_to", "applies_to_tier", "active"):
            assert row[column] == original[column], column
        assert row["value"] == original["value"]
        assert row["start_date"] == _as_date(original["start_date"]) and row["end_date"] == _as_date(original["end_date"])
        assert row["created_at"] == parse_datetime(original["created_at"])


def test_legacy_calculator_lookups_agree(imported):
    source = [{**row, "size_rates": dict(row["size_rates"])} for row in goldenapp("bom_marketrate")]
    rebuilt = legacy_rebuild.market_rates()
    sizes = {"ongrid": ["3", "5sp", "5tp", "6", "8", "10", "4"], "hybrid": ["3", "5", "8", "10"]}
    checked = 0
    for sys_type, keys in sizes.items():
        for tier in ("base", "value", "premium"):
            for bat in ("0", "1", "2", ""):
                for size in keys:
                    assert _lookup_market_rate(rebuilt, sys_type, tier, bat, size) == _lookup_market_rate(source, sys_type, tier, bat, size)
                    checked += 1
    for frm, to in ((3, 5), (5, 8), (5, 10), (8, 10), (3, 10)):
        assert _lookup_upgrade(rebuilt, frm, to) == _lookup_upgrade(source, frm, to)
    assert _lookup_market_rate(rebuilt, "ongrid", "base", "0", "3") == 202539.3
    offers_source = [{**row, "start_date": _as_date(row["start_date"]), "end_date": _as_date(row["end_date"])} for row in goldenapp("bom_offer")]
    for today in (date(2026, 9, 28), date(2025, 2, 1), date(2027, 1, 1)):
        for sys_type in ("ongrid", "hybrid", "upgrade"):
            for tier in ("base", "value", "premium"):
                assert _offers_for(legacy_rebuild.offers(), sys_type, tier, today) == _offers_for(offers_source, sys_type, tier, today)
    assert checked == 132


def test_catalog_item_prices_rebuild_exactly():
    result = catalog_fixtures.import_all(order=("bom",))["bom"]
    legacy_import.import_prices(result["prices"])
    rebuilt = legacy_rebuild.catalog_item_prices()
    items = catalog_fixtures.goldenapp("bom_catalogitem")
    assert len(rebuilt) == len(items) == 257
    for item in items:
        assert rebuilt[item["id"]] == {"price": item["price"], "per_watt": item["per_watt"]}, item["item_id"]
