"""Calculators, EMI and the BOM quote through the shim, replaying the recorded legacy corpora (the committed goldens of
the calculators-emi and bom packages) with the LEGACY request bodies — integer EMI size ids included — and comparing
the LEGACY response bodies: same status, same JSON (integers and floats apart), ``{"error": message}`` errors.
Approved: a legacy HTTP 500 is a 400 ``{"error": …}``; the quote omits ``cost_breakdown``/``totals`` (B-1)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from freezegun import freeze_time

from bom.services.website_quote import INTERNAL_KEYS
from bom.tests.legacy_fixtures import GOLDEN_DAY, golden_cases, import_legacy_database
from calculators.tests.parity.support import battery_price_provider, import_legacy_rows, load_corpus, load_rows, typed
from catalog.services import pricing_hooks
from emi.services import legacy_import as emi_import
from legacy.tests.conftest import ordered

pytestmark = pytest.mark.django_db
EMI_PARITY = Path(__file__).resolve().parents[2] / "emi" / "tests" / "parity"
CALCULATORS = {"basic": "/legacy/api/calculate-solar/", "basic_v2": "/legacy/api/calculate-solar-new/", "advanced": "/legacy/api/calculate-solar-advanced/"}
EMI = {"calculate": "/legacy/api/emi-calculator/", "quotation": "/legacy/api/emi-calculator/quotation/"}


def replay(api_client, path: str, case: dict) -> str | None:
    text = case["request"] if isinstance(case["request"], str) else json.dumps(case["request"])
    response = api_client.post(path, data=text.encode("utf-8"), content_type="application/json")
    body = ordered(response) if response.content else None
    if case["status"] >= 500:
        return None if response.status_code == 400 and list(body) == ["error"] else f"legacy 500 → {response.status_code} {body}"
    if response.status_code != case["status"]:
        return f"status {case['status']} → {response.status_code} {body}"
    if typed(body) != typed(case["response"]) or (isinstance(body, dict) and list(body) != list(case["response"])):
        return f"body differs: {json.dumps(case['response'])[:300]} → {json.dumps(body)[:300]}"
    return None


@pytest.fixture
def calculator_tables():
    previous = pricing_hooks.current_provider()
    pricing_hooks.register(battery_price_provider(import_legacy_rows(load_rows("uat"))))
    yield
    pricing_hooks.register(previous)


@pytest.mark.parametrize("key", list(CALCULATORS))
def test_calculators_answer_as_the_legacy_endpoints(api_client, calculator_tables, key):
    cases = load_corpus("uat", key)
    differences = [f"{case['name']}: {difference}" for case in cases if (difference := replay(api_client, CALCULATORS[key], case))]
    assert not differences, f"{len(differences)} of {len(cases)}:\n" + "\n".join(differences[:20])


def test_calculator_answers_are_not_cached(api_client, calculator_tables):
    case = next(case for case in load_corpus("uat", "basic") if case["status"] == 200)
    response = api_client.post(CALCULATORS["basic"], data=case["request"], content_type="application/json")
    assert response["Cache-Control"] == "no-store"


@pytest.fixture
def emi_rows():
    rows = json.loads((EMI_PARITY / "uat" / "legacy_rows.json").read_text())["rows"]
    emi_import.import_all(
        banks=rows["emi_bank"],
        interest_rules=rows["emi_interest_rate_rule"],
        subsidy_rules=rows["emi_subsidy_rule"],
        settings=rows["emi_calculator_settings"],
        system_sizes=rows["emi_system_size"],
    )


@pytest.mark.parametrize("key", list(EMI))
def test_emi_answers_as_the_legacy_endpoints_with_integer_ids(api_client, emi_rows, key):
    cases = json.loads((EMI_PARITY / "uat" / f"corpus_{key}.json").read_text())["cases"]
    differences = [f"{case['name']}: {difference}" for case in cases if (difference := replay(api_client, EMI[key], case))]
    assert not differences, f"{len(differences)} of {len(cases)}:\n" + "\n".join(differences[:20])


def test_emi_config_is_the_legacy_payload(api_client, emi_rows, django_assert_max_num_queries):
    legacy = json.loads((EMI_PARITY / "uat" / "config.json").read_text())["response"]
    with django_assert_max_num_queries(8):
        response = api_client.get("/legacy/api/emi-calculator/config/")
    body = ordered(response)
    assert list(body["settings"]) == list(legacy["settings"]) and body == legacy
    assert api_client.get("/legacy/api/emi-calculator/config/")["X-Cache"] == "HIT"


def test_unknown_or_malformed_size_ids_answer_like_the_legacy(api_client, emi_rows):
    unknown = api_client.post(EMI["calculate"], {"size_id": 999, "tenure_years": 5}, format="json")
    malformed = api_client.post(EMI["calculate"], {"size_id": "abc", "tenure_years": 5}, format="json")
    assert unknown.status_code == 400 and list(unknown.json()) == ["error"] and "size_uid" not in unknown.json()["error"]
    assert malformed.status_code == 400 and list(malformed.json()) == ["error"]


# ── BOM quote (DV-4, B-1) ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def bom_tables():
    return import_legacy_database()


def test_bom_quote_answers_as_the_legacy_endpoint_without_internal_costs(api_client, bom_tables):
    golden = golden_cases()["cases"]
    sample = [case for case in golden if case["status"] == 200][::25] + [case for case in golden if case["status"] == 400]
    with freeze_time(f"{GOLDEN_DAY} 12:00:00"):
        for case in sample:
            response = api_client.post("/legacy/bom/api/calculate/", case["request"], format="json")
            body = ordered(response)
            assert response.status_code == case["status"], case["request"]
            if case["status"] == 200:
                expected = {key: value for key, value in case["response"].items() if key not in INTERNAL_KEYS}
                assert not set(INTERNAL_KEYS) & set(body)
                assert json.dumps(body) == json.dumps(expected), case["request"]
                assert response["Cache-Control"] == "no-store"
            else:
                assert body == case["response"]  # {"errors": [...]} — the legacy messages in the legacy order


def test_bom_quote_legacy_500_is_a_400_with_the_legacy_errors_shape(api_client, bom_tables):
    request = {"sys_type": "ongrid", "size": "3", "tier": "base", "bat_config": "0", "subsidy_type": "residential", "custom_discount": 7500}
    with freeze_time(f"{GOLDEN_DAY} 12:00:00"):
        response = api_client.post("/legacy/bom/api/calculate/", request, format="json")
    assert response.status_code == 400 and list(response.json()) == ["errors"]


def test_bom_quote_without_configuration_is_the_legacy_error_shape(api_client):
    response = api_client.post("/legacy/bom/api/calculate/", {"sys_type": "ongrid", "size": "3", "tier": "base"}, format="json")
    assert response.status_code in (400, 503) and list(response.json()) == ["error"]


def test_golden_day_is_a_date():
    assert date.fromisoformat(GOLDEN_DAY)
