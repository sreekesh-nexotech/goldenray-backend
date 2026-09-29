"""Parity of the three website calculators with the legacy endpoints (PLAN §4.5 "200 recorded inputs", §7.6 #10).

The corpora in ``parity/<variant>/`` were recorded from the legacy main backend by ``parity/capture_legacy.py``:
``uat`` from the shared UAT server (read-only; the calculators only read), ``enriched`` from a private restored
copy with ``parity/enrich_private.sql`` applied. The same legacy rows are imported through the owners' importers and
every recorded request is replayed against the canonical endpoint. Approved differences (docs/decisions/
calculators-emi.md): a legacy 500 is a 400 ``invalid_input``; errors use the platform envelope (same status, the
legacy text as ``message``). Nothing else may differ — not even ``1`` versus ``1.0``.
"""

from __future__ import annotations

import pytest

from calculators.tests.parity.support import ENDPOINTS, battery_price_provider, compare, import_legacy_rows, load_corpus, load_rows
from catalog.services import pricing_hooks

pytestmark = pytest.mark.django_db
VARIANTS = [variant for variant in ("uat", "enriched") if (__import__("pathlib").Path(__file__).parent / "parity" / variant).is_dir()]


@pytest.fixture
def legacy_tables(request):
    variant = request.param
    previous = pricing_hooks.current_provider()
    pricing_hooks.register(battery_price_provider(import_legacy_rows(load_rows(variant))))
    yield variant
    pricing_hooks.register(previous)


@pytest.mark.parametrize("legacy_tables", VARIANTS, indirect=True)
@pytest.mark.parametrize("key", list(ENDPOINTS))
def test_every_recorded_request_answers_as_the_legacy_endpoint(api_client, legacy_tables, key):
    cases = load_corpus(legacy_tables, key)
    assert len(cases) >= 200 or legacy_tables != "uat", f"{key}: the UAT corpus must hold at least 200 inputs"
    differences = []
    for case in cases:
        response = api_client.post(ENDPOINTS[key], data=case["request"].encode("utf-8"), content_type="application/json")
        body = response.json() if response.content else None
        difference = compare(case, response.status_code, body)
        if difference:
            differences.append(f"{case['name']}: {difference}")
    assert not differences, f"{len(differences)} of {len(cases)} {key} cases differ:\n" + "\n".join(differences[:40])


def test_the_uat_corpus_covers_every_outcome():
    """The grid reaches every legacy outcome: 200, the 400/404 messages and the 500s (→ 400)."""
    outcomes = {key: {case["status"] for case in load_corpus("uat", key)} for key in ENDPOINTS}
    assert outcomes["basic"] >= {200, 400, 404, 500}
    assert outcomes["basic_v2"] >= {200, 400, 404, 500}
    assert outcomes["advanced"] >= {200, 400, 500}
    messages = {case["response"]["error"] for key in ENDPOINTS for case in load_corpus("uat", key) if case["status"] in (400, 404) and case["response"]}
    assert {
        "Missing required fields",
        "Invalid monthly bill",
        "Pincode not found in database",
        "Invalid monthly_bill value",
        "Monthly bill out of supported range",
        "No data found for the given bill range and property type",
        "Only On Grid and Hybrid supported in this version.",
    } <= messages
