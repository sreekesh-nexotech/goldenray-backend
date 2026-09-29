"""The real Flarize data through the platform importers, for the quotation parity tests.

* catalog, prices, BOM and pack configuration (+ registry pins): ``packs.tests.flarize`` (committed fixtures);
* the engine configurations and the validity policy: ``pricing/tests/fixtures/legacy/flarize_*.json`` (identical to
  Flarize ``data/*.json``);
* quotation content, inclusions, tier names, testimonials (masked): ``quotations/tests/fixtures/flarize``;

then PriceRelease #1 and PackRelease #1 are published through the services. ``golden()`` is the expected result of the
real JavaScript (``quotations/tests/golden/generate_quotations.mjs``).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
PRICING = ROOT / "pricing" / "tests" / "fixtures" / "legacy"
GOLDEN = Path(__file__).parent / "golden" / "flarize_quotations.json"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pricing_fixture(name: str):
    return json.loads((PRICING / f"flarize_{name}.json").read_text(encoding="utf-8"))


@cache
def _golden_text() -> str:
    return GOLDEN.read_text(encoding="utf-8")


def golden() -> dict:
    return json.loads(_golden_text())


def import_content(user=None) -> None:
    from quotations.services import legacy_import

    legacy_import.import_flarize_inclusions(fixture("flarize/quotation-inclusions.json"), user=user)
    legacy_import.import_flarize_tier_names(fixture("flarize/tier-display-names.json"), user=user)
    legacy_import.import_flarize_testimonials(fixture("flarize/quotation-testimonials.json"), user=user)
    legacy_import.import_flarize_content(fixture("flarize/quotation-content.json"), user=user)


def import_world(user=None) -> dict:
    """Everything a quotation needs, then PriceRelease #1 and PackRelease #1 published."""
    from packs.services.legacy_import import import_flarize_pack_config, publish_initial_releases
    from packs.tests import flarize as packs_flarize
    from pricing.services import legacy_import as pricing_import

    packs_flarize.import_catalog_and_prices(user)
    pricing_import.import_flarize_documents(
        quotation_policy_json=pricing_fixture("quotation-policy"),
        engine_configs={name: pricing_fixture(f"{name}-config") for name in ("energy", "savings", "subsidy", "finance")},
        user=user,
    )
    import_flarize_pack_config(packs_flarize.pack_store(), packs_flarize.registry(), user=user)
    published = publish_initial_releases(user=user)
    import_content(user)
    return published
