"""Committed legacy fixtures of the bom package (exported read-only; no personal data in these tables).

* ``fixtures/legacy/goldenapp_<table>.json`` — ``json_agg(row_to_json(t) ORDER BY id)`` of the UAT main-backend
  tables ``bom_bomtemplate``, ``bom_bomslot``, ``bom_bomfixeditem``, ``bom_structuretemplate``,
  ``bom_structuretemplateitem``, ``bom_tubeweight`` (from a private restore of
  ``/home/user/platform-reference/uat/legacy_goldenapp.dump``; numerics keep their scale via ``parse_float=Decimal``,
  JSON columns keep the jsonb key order the legacy calculator iterated);
* ``fixtures/legacy/flarize_catalog_bom.json`` — the ``bomTemplates``, ``structureTemplates``, ``tubeWeights`` and
  ``packageProfiles`` sections of Flarize ``data/catalog.json``;
* ``golden/bom_calculate.json.gz`` — the 1,077 request/response pairs captured from the legacy
  ``POST /bom/api/calculate/`` on 2026-09-28 (``platform-reference/uat/golden/bom_calculate.json``, gzipped).

The catalog (``bom_category``, ``bom_catalogitem``, ``bom_itemtier``) and pricing (``bom_globalcosts``,
``bom_marketrate``, ``bom_offer``) rows of the same database are the catalog and pricing packages' committed fixtures
(identical to the dump, checked when these were exported).
"""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "legacy"
GOLDEN = HERE / "golden" / "bom_calculate.json.gz"
GOLDEN_DAY = "2026-09-28"


def goldenapp(table: str, *, decimals: bool = True) -> list[dict]:
    text = (FIXTURES / f"goldenapp_{table}.json").read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal) if decimals else json.loads(text)


def flarize_catalog_bom() -> dict:
    return json.loads((FIXTURES / "flarize_catalog_bom.json").read_text(encoding="utf-8"))


def golden_cases() -> dict:
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def goldenray_rows() -> tuple[list[dict], ...]:
    """The six main-backend tables in :func:`bom.services.legacy_import.import_goldenray_bom` argument order.

    JSON columns are read without ``parse_float=Decimal`` (the legacy calculator got floats from jsonb)."""
    tables = ("bom_template", "bom_slot", "bom_fixeditem", "bom_structuretemplate", "bom_structuretemplateitem", "bom_tubeweight")
    json_columns = {"sizes", "three_phase_sizes", "available_tiers", "battery_configs", "qty", "premium_qty", "bat_qty", "premium_bat_qty"}
    result = []
    for table in tables:
        rows = goldenapp(table)
        floats = goldenapp(table, decimals=False)
        result.append([{key: (plain[key] if key in json_columns else value) for key, value in row.items()} for row, plain in zip(rows, floats, strict=True)])
    return tuple(result)


def import_legacy_database(user=None, *, flarize: bool = False) -> dict:
    """Load the UAT legacy database through the catalog, pricing and bom importers (the golden data set), activate
    the imported market-rate set; ``flarize=True`` also runs the Flarize bom import afterwards."""
    from bom.services import legacy_import
    from catalog.tests import legacy_fixtures as catalog_fixtures
    from pricing.models import MarketRateSet
    from pricing.services import legacy_import as pricing_import
    from pricing.services.market_rates import activate_set
    from pricing.tests import legacy_fixtures as pricing_fixtures

    results = {"catalog": catalog_fixtures.import_all(user, order=("bom",))["bom"]}
    results["prices"] = pricing_import.import_prices(results["catalog"]["prices"], user=user)
    results["costs"] = pricing_import.import_bom_global_costs(pricing_fixtures.goldenapp("bom_globalcosts"), user=user)
    results["rates"] = pricing_import.import_bom_market_rates(pricing_fixtures.goldenapp("bom_marketrate"), user=user)
    results["offers"] = pricing_import.import_bom_offers(pricing_fixtures.goldenapp("bom_offer"), user=user)
    for rate_set in MarketRateSet.objects.exclude(status="ACTIVE"):
        activate_set(rate_set, user=user)
    results["bom"] = legacy_import.import_goldenray_bom(*goldenray_rows(), user=user)
    if flarize:
        results["flarize"] = legacy_import.import_flarize_bom(flarize_catalog_bom(), user=user)
    return results
