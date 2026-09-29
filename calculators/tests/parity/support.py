"""Load a captured legacy corpus into the platform and compare responses (used by ``test_parity.py``).

:func:`import_legacy_rows` imports the rows the legacy calculators read, each through its owner's importer: the
reference lists (``reference.services.legacy_import``), the sizing tables (``calculators.services.legacy_import``) and
the website batteries (``catalog.services.legacy_import.import_goldenray_products`` — public battery products). The
batteries' prices are what that importer returns for the pricing package; :func:`battery_price_provider` stands in
for the pricing package's provider (``catalog.services.pricing_hooks``) with exactly those prices.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from calculators.services import legacy_import as sizing_import
from catalog.models import Component
from catalog.services import legacy_import as catalog_import
from catalog.services import pricing_hooks
from reference.services import legacy_import as reference_import

HERE = Path(__file__).resolve().parent
ENDPOINTS = {"basic": "/api/public/v1/calculators/basic/", "basic_v2": "/api/public/v1/calculators/basic-v2/", "advanced": "/api/public/v1/calculators/advanced/"}


def load_rows(variant: str) -> dict:
    return json.loads((HERE / variant / "legacy_rows.json").read_text())["rows"]


def load_corpus(variant: str, key: str) -> list[dict]:
    return json.loads((HERE / variant / f"corpus_{key}.json").read_text())["cases"]


def import_legacy_rows(rows: dict) -> dict:
    """Import every table and return ``{component pk: battery price}`` for the price provider."""
    reference_import.import_tariffs(rows["kseb_tariffs"])
    reference_import.import_device_types(rows["device_types"])
    reference_import.import_ev_cars(rows["ev_cars"])
    reference_import.import_ev_scooters(rows["ev_scooters"])
    reference_import.import_pincodes(rows["pincodes"])
    sizing_import.import_all(solar_installations=rows["solar_installations"], solar_installation_new=rows["solar_installation_new"])
    result = catalog_import.import_goldenray_products([], [], rows["batteries"])
    assert not [violation for violation in result["violations"] if violation.get("severity") == "error"], result["violations"]
    prices = {price["sku"]: Decimal(str(price["amount"])) for price in result["prices"] if price["kind"] == "LIST"}
    return {component.pk: prices[component.sku] for component in Component.objects.filter(sku__in=prices)}


def battery_price_provider(prices: dict):
    def provider(components):
        return {component.pk: pricing_hooks.PriceInfo(min_amount=prices[component.pk], max_amount=prices[component.pk]) for component in components if component.pk in prices}

    return provider


def typed(value):
    """A JSON value with integers and floats kept apart (``1`` is not ``1.0`` for parity) and floats by ``repr``."""
    if isinstance(value, dict):
        return {key: typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [typed(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, int):
        return ("int", value)
    return value


def compare(case: dict, status: int, body, *, canonical=None, message=None) -> str | None:
    """``None`` when the platform answered as the legacy did (approved differences applied), else the difference.

    * legacy 500 → platform 400 ``invalid_input``;
    * legacy 4xx ``{"error": m}`` → the same status with ``message`` == ``m`` (after ``message`` rewrites);
    * legacy 200 → the same JSON, integers and floats kept apart (after ``canonical`` rewrites).
    """
    legacy_status, legacy_body = case["status"], case["response"]
    if legacy_status >= 500:
        if status == 400 and isinstance(body, dict) and body.get("code") == "invalid_input":
            return None
        return f"legacy {legacy_status} ({case.get('error_class')}) → platform {status} {body}"
    if status != legacy_status:
        return f"legacy {legacy_status} {legacy_body} → platform {status} {body}"
    if legacy_status != 200:
        expected = legacy_body.get("error") if isinstance(legacy_body, dict) else None
        expected = message(expected) if message and expected else expected
        got = body.get("message") if isinstance(body, dict) else None
        return None if expected == got else f"message {expected!r} → {got!r}"
    expected = canonical(legacy_body) if canonical else legacy_body
    return None if typed(expected) == typed(body) else f"body differs:\n  legacy   {json.dumps(expected)[:900]}\n  platform {json.dumps(body)[:900]}"
