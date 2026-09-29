"""Committed legacy fixtures (exported read-only from the UAT legacy sources; no personal data in these tables).

* ``goldenapp_<table>.json`` — ``json_agg(row_to_json(t) ORDER BY id)`` of the main backend tables ``solar_panels``,
  ``solar_inverters``, ``batteries``, ``bom_category``, ``bom_catalogitem``, ``bom_itemtier`` (numerics keep their
  scale: loaded with ``parse_float=Decimal``, as a psycopg row would deliver them);
* ``flarize_catalog.json`` — the ``categories`` section of Flarize ``data/catalog.json``; ``flarize_battery_master.json``
  — ``data/battery-master.json`` (the Flarize ids ``admin-001`` are internal user ids, not personal data);
* ``response_<endpoint>.json`` — the legacy server's ``GET /api/solar-panels/``, ``/api/solar-inverters/``,
  ``/api/batteries/`` responses for the same data (the parity evidence for the legacy shim).
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import cache
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "legacy"


def _load(name: str, *, decimals: bool):
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal) if decimals else json.load(handle)


@cache
def _cached(name: str, decimals: bool):
    return json.dumps(_load(name, decimals=decimals), default=str)


def goldenapp(table: str) -> list[dict]:
    return _load(f"goldenapp_{table}.json", decimals=True)


def flarize_catalog() -> dict:
    return _load("flarize_catalog.json", decimals=False)


def flarize_battery_master() -> dict:
    return _load("flarize_battery_master.json", decimals=False)


def legacy_response(endpoint: str):
    return _load(f"response_{endpoint}.json", decimals=False)


def import_all(user=None, *, order=("flarize", "bom", "website")) -> dict:
    """Run the three importers over the committed fixtures in ``order``; returns ``{name: result}``."""
    from catalog.services import legacy_import

    results = {}
    for name in order:
        if name == "flarize":
            results[name] = legacy_import.import_flarize_catalog(flarize_catalog(), flarize_battery_master(), user=user)
        elif name == "bom":
            results[name] = legacy_import.import_bom_catalog(goldenapp("bom_category"), goldenapp("bom_catalogitem"), goldenapp("bom_itemtier"), user=user)
        else:
            results[name] = legacy_import.import_goldenray_products(goldenapp("solar_panels"), goldenapp("solar_inverters"), goldenapp("batteries"), user=user)
    return results
