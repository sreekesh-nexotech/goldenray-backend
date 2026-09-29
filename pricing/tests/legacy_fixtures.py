"""Committed legacy fixtures of the pricing/procurement imports (exported read-only; no personal data).

* ``goldenapp_<table>.json`` — ``json_agg(row_to_json(t) ORDER BY id)`` of the UAT main backend tables
  ``bom_globalcosts``, ``bom_marketrate``, ``bom_offer`` (numerics keep their scale: loaded with
  ``parse_float=Decimal``, as psycopg rows deliver them); the ``bom_catalogitem`` prices come from the catalog package's
  fixture (``catalog/tests/fixtures/legacy/goldenapp_bom_catalogitem.json``);
* ``flarize_catalog_pricing.json`` — the pricing sections of Flarize ``data/catalog.json`` (``costs``,
  ``installationMatrix``, ``transportConfig``, ``officeExpense``, ``marketRates``, ``offers``, ``_legacyMarkers``);
* ``flarize_<file>`` — ``cost-config.json``, ``project-rate-card.json``, ``quotation-policy.json`` and the four engine
  configurations, verbatim (Flarize user ids such as ``admin-001`` are internal ids, not personal data);
* ``procurement/tests/fixtures/legacy/flarize_<file>`` — ``procurement-state.json``, ``commercial-history.json``,
  ``procurement-price-master.json`` (minified, otherwise verbatim).

The Purchase Agreement ``kseb`` list is the page's built-in option list (``KSEB_FEES`` in ``index.html``).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

PRICING = Path(__file__).parent / "fixtures" / "legacy"
PROCUREMENT = Path(__file__).resolve().parents[2] / "procurement" / "tests" / "fixtures" / "legacy"
KSEB_FEES = [
    {"v": "5400", "l": "3 KW — ₹ 5,400"},
    {"v": "7800", "l": "5 KW — ₹ 7,800"},
    {"v": "11240", "l": "8 KW — ₹ 11,240"},
    {"v": "13600", "l": "10 KW — ₹ 13,600"},
    {"v": "15960", "l": "12 KW — ₹ 15,960"},
    {"v": "19500", "l": "15 KW — ₹ 19,500"},
]


def goldenapp(table: str) -> list[dict]:
    return json.loads((PRICING / f"goldenapp_{table}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def flarize_catalog_pricing() -> dict:
    return json.loads((PRICING / "flarize_catalog_pricing.json").read_text(encoding="utf-8"))


def flarize_file(name: str) -> dict:
    return json.loads((PRICING / f"flarize_{name}").read_text(encoding="utf-8"))


def flarize_procurement(name: str) -> dict:
    return json.loads((PROCUREMENT / f"flarize_{name}").read_text(encoding="utf-8"))


def engine_configs() -> dict:
    return {name: flarize_file(f"{name}-config.json") for name in ("energy", "savings", "subsidy", "finance")}
