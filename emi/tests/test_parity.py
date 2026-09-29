"""Parity of the EMI calculator with the legacy ``emi-calculator/``, ``…/config/`` and ``…/quotation/`` endpoints.

The corpora in ``parity/<variant>/`` were recorded by ``parity/capture_legacy.py`` (``uat``: the shared UAT server;
``enriched``: a private restored copy with ``parity/enrich_private.sql``). The legacy EMI rows are imported through
``emi.services.legacy_import`` and every request is replayed against the canonical endpoint after the id → uid
translation the shim will do (``parity/translate.py``). Approved differences (docs/decisions/calculators-emi.md):
``uid`` for ids, a legacy 500 is a 400 ``invalid_input``, errors in the platform envelope, and the config's settings
carry the new ``disclaimer_en``/``disclaimer_ml`` (empty after the import). Nothing else may differ.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calculators.tests.parity.support import compare
from emi.services import legacy_import
from emi.tests.parity import translate

pytestmark = pytest.mark.django_db
HERE = Path(__file__).resolve().parent / "parity"
VARIANTS = [variant for variant in ("uat", "enriched") if (HERE / variant).is_dir()]
ENDPOINTS = {"calculate": "/api/public/v1/calculators/emi/", "quotation": "/api/public/v1/calculators/emi/quotation/"}


def _rows(variant: str) -> dict:
    return json.loads((HERE / variant / "legacy_rows.json").read_text())["rows"]


def _import(variant: str) -> dict:
    rows = _rows(variant)
    results = legacy_import.import_all(
        banks=rows["emi_bank"],
        interest_rules=rows["emi_interest_rate_rule"],
        subsidy_rules=rows["emi_subsidy_rule"],
        settings=rows["emi_calculator_settings"],
        system_sizes=rows["emi_system_size"],
    )
    for table, result in results.items():
        assert not result["violations"], (table, result["violations"])
    return translate.uid_maps()


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("key", list(ENDPOINTS))
def test_every_recorded_request_answers_as_the_legacy_endpoint(api_client, variant, key):
    maps = _import(variant)
    cases = json.loads((HERE / variant / f"corpus_{key}.json").read_text())["cases"]
    assert len(cases) >= 200 or variant != "uat", f"{key}: the UAT corpus must hold at least 200 inputs"
    differences = []
    for case in cases:
        text = translate.dumps(translate.request(translate.loads(case["request"]), maps)) if key == "calculate" else case["request"]
        response = api_client.post(ENDPOINTS[key], data=text.encode("utf-8"), content_type="application/json")
        got = response.json() if response.content else None
        difference = compare(case, response.status_code, got, canonical=lambda legacy: translate.response(legacy, maps), message=translate.message)
        if difference:
            differences.append(f"{case['name']}: {difference}")
    assert not differences, f"{len(differences)} of {len(cases)} {key} cases differ:\n" + "\n".join(differences[:40])


@pytest.mark.parametrize("variant", VARIANTS)
def test_config_answers_as_the_legacy_endpoint(api_client, variant):
    maps = _import(variant)
    legacy = json.loads((HERE / variant / "config.json").read_text())["response"]
    expected = {
        "settings": {**legacy["settings"], "disclaimer_en": "", "disclaimer_ml": ""},
        "system_sizes": translate.response(legacy["system_sizes"], maps, table="emi_system_size"),
        "banks": translate.response(legacy["banks"], maps, table="emi_bank"),
    }
    response = api_client.get("/api/public/v1/calculators/emi/config/")
    assert response.status_code == 200
    assert compare({"status": 200, "response": expected}, 200, response.json()) is None, json.dumps(response.json())[:2000]
