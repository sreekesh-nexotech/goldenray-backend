"""The real Flarize data through the platform importers (catalog → pricing → bom → packs).

Inputs are committed fixtures exported read-only from ``/home/user/flarize-main/flarize/data`` (no personal data; the
ids ``admin-001`` are Flarize's internal user ids):

* ``engines/tests/golden/fixtures/flarize_commercial.json`` — ``catalog.json`` (whole), ``pack-config.json`` (whole
  store: approved v16, draft v17, history), ``packages.proposed.json`` reduced to the first package per (system type,
  size, tier, phase) — the only record ``buildBom`` reads, and so the only one the pins come from —, and
  ``battery-master.json`` ``batteries``;
* ``pricing/tests/fixtures/legacy/flarize_cost-config.json`` — the GST split (PriceRelease GST keys).

``packs/tests/golden/flarize_packs.json`` is the expected result, produced by the real JavaScript
(``packs/tests/golden/generate_packs.mjs``).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMERCIAL = ROOT / "engines" / "tests" / "golden" / "fixtures" / "flarize_commercial.json"
COST_CONFIG = ROOT / "pricing" / "tests" / "fixtures" / "legacy" / "flarize_cost-config.json"
GOLDEN = Path(__file__).parent / "golden" / "flarize_packs.json"


@cache
def _commercial_text() -> str:
    return COMMERCIAL.read_text(encoding="utf-8")


def commercial() -> dict:
    return json.loads(_commercial_text())


def pack_store() -> dict:
    return commercial()["packStore"]


def registry() -> dict:
    return commercial()["registry"]


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def import_catalog_and_prices(user=None) -> None:
    from bom.services import legacy_import as bom_import
    from catalog.services import legacy_import as catalog_import
    from pricing.services import legacy_import as pricing_import

    data = commercial()
    result = catalog_import.import_flarize_catalog(data["catalog"], {"batteries": data["batteryMaster"]}, user=user)
    pricing_import.import_prices(result["prices"], user=user)
    pricing_import.import_flarize_pricing(data["catalog"], user=user)
    pricing_import.import_flarize_documents(cost_config_json=json.loads(COST_CONFIG.read_text(encoding="utf-8")), user=user)
    bom_import.import_flarize_bom(data["catalog"], user=user)


def import_world(user=None) -> dict:
    """Everything up to the imported pack configuration (no release published)."""
    from packs.services import legacy_import

    import_catalog_and_prices(user)
    return legacy_import.import_flarize_pack_config(pack_store(), registry(), user=user)
