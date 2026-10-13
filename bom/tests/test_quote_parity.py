"""``PER_LINE`` regression pin: the quote under the default GST regime is unchanged.

Originally the parity suite for the legacy ``POST /bom/api/calculate/``. The BOM port
(see ``test_source_parity.py``) is adopting Golden-Ray-BOM's pricing, which differs — flat
composite GST, flat-roof structure as material, margin on pre-GST cost — so this file no
longer defines what the engine *should* do. It now guards something narrower and more
useful: that the new behaviour is reachable **only** through ``gst.quote_regime``, and
that the regime's absence leaves every one of these 1,059 responses exactly as it was.

That matters because ``website_quote.quote`` also serves the public website
(``legacy/services/website.py``) and the public ``QuoteView``. Without this pin, a
deployment could silently re-price live customer quotes.

The comparison allows exactly one difference: keys the port ADDS to ``totals``
(:data:`ADDED_TOTALS_KEYS`). Every other key, its order, and every value — with number
types significant, ``13122.0`` is not ``13122`` — must match the captured body. The 9
legacy HTTP 500 cases (``custom_discount`` sent as a number) remain approved differences:
a 400 ``validation_error``.
"""

import json
from datetime import date

import pytest

from bom.services.website_quote import INTERNAL_KEYS, QuoteInvalid, quote
from bom.tests.legacy_fixtures import GOLDEN_DAY, golden_cases, import_legacy_database

pytestmark = pytest.mark.django_db
TODAY = date.fromisoformat(GOLDEN_DAY)

#: Keys the BOM port adds to ``totals``. Additive by design: the frontend's ``BomTotals``,
#: the quotations snapshots and ``packs`` all read this object, so nothing is removed.
#: Under PER_LINE ``pre_gst_subtotal`` and ``gst_on_sale`` are null (there is no separate
#: charge on the sale) and ``gst_total`` restates the sum of the three buckets.
ADDED_TOTALS_KEYS = ("gst_total", "pre_gst_subtotal", "gst_on_sale")


def _dumped(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _without_added_keys(body: dict) -> dict:
    """``body`` as the golden capture would have had it, before the port's new keys."""
    if "totals" not in body:
        return body
    return {**body, "totals": {k: v for k, v in body["totals"].items() if k not in ADDED_TOTALS_KEYS}}


@pytest.fixture
def legacy_data():
    return import_legacy_database()


def _replay(case):
    try:
        return 200, quote(case["request"], today=TODAY)
    except QuoteInvalid as exc:
        return 400, {"errors": exc.legacy_errors}


def test_every_captured_response_is_identical(legacy_data):
    """Default regime (no ``gst.quote_regime`` row) reproduces every captured body."""
    golden = golden_cases()
    assert golden["captured_on"] == GOLDEN_DAY and len(golden["cases"]) == 1077
    mismatches, statuses, approved = [], {200: 0, 400: 0}, 0
    for index, case in enumerate(golden["cases"]):
        status, body = _replay(case)
        if case["status"] == 500:
            assert status == 400 and isinstance(case["request"].get("custom_discount"), (int, float)), case["request"]
            approved += 1
            continue
        statuses[case["status"]] += 1
        if status != case["status"] or _dumped(_without_added_keys(body)) != _dumped(case["response"]):
            mismatches.append((index, case["request"]))
    assert not mismatches, f"{len(mismatches)} differing cases, first: {mismatches[:3]}"
    assert statuses == {200: 1059, 400: 9} and approved == 9


def test_the_added_totals_keys_are_present_and_inert_under_per_line(legacy_data):
    """The new keys exist on every quote, and under PER_LINE say "no composite charge".

    Asserted separately so :func:`test_every_captured_response_is_identical` cannot go
    green by the keys simply being absent.
    """
    body = quote({"sys_type": "ongrid", "size": "3", "tier": "base"}, today=TODAY)
    totals = body["totals"]
    assert set(ADDED_TOTALS_KEYS) <= set(totals), f"missing {sorted(set(ADDED_TOTALS_KEYS) - set(totals))}"
    assert totals["pre_gst_subtotal"] is None and totals["gst_on_sale"] is None
    assert totals["gst_total"] == totals["gst_5"] + totals["gst_12"] + totals["gst_18"]
    assert totals["material_with_gst"] == totals["material_ex_gst"] + totals["gst_total"]


def test_composite_is_reachable_only_through_the_cost_config_key(legacy_data):
    """Switching the regime changes the ladder; nothing else does.

    The companion to the pin above: together they say the port's pricing is opt-in.
    """
    from pricing.services.cost_config import put_values

    request = {"sys_type": "ongrid", "size": "3", "tier": "base"}
    before = quote(request, today=TODAY)
    # The legacy fixture carries no GST split, and COMPOSITE refuses to guess a rate —
    # so seed the standard 70/30 at 5/18 that gives the 8.9 % effective rate.
    put_values(
        user=None,
        entries=[
            {"key": "gst_goods_share", "value": "0.70"},
            {"key": "gst_services_share", "value": "0.30"},
            {"key": "gst_goods_rate", "value": "0.05"},
            {"key": "gst_services_rate", "value": "0.18"},
            {"key": "gst.quote_regime", "value": {"mode": "COMPOSITE"}},
        ],
    )
    after = quote(request, today=TODAY)

    assert before["totals"]["pre_gst_subtotal"] is None
    assert after["totals"]["pre_gst_subtotal"] is not None, "COMPOSITE did not take effect"
    # Flat-roof structure moves from an overhead into the material lines.
    assert not any(line["section"] == "structure" for line in before["bom_lines"])
    assert any(line["section"] == "structure" for line in after["bom_lines"])
    # The per-category buckets stop being used (no custom items in this request).
    assert after["totals"]["gst_5"] == after["totals"]["gst_12"] == after["totals"]["gst_18"] == 0
    assert after["totals"]["gst_total"] > 0
    # structure_base is still reported, but is no longer inside cost_total.
    assert after["cost_breakdown"]["structure_base"] > 0
    assert after["cost_breakdown"]["cost_total"] < before["cost_breakdown"]["cost_total"]


def test_the_legacy_500_cases_are_validation_errors(legacy_data):
    request = {"sys_type": "ongrid", "size": "3", "tier": "base", "bat_config": "0", "subsidy_type": "residential", "custom_discount": 7500}
    with pytest.raises(QuoteInvalid) as caught:
        quote(request, today=TODAY)
    assert caught.value.code == "validation_error" and "custom_discount" in caught.value.errors


def test_the_public_endpoint_serves_the_same_json_without_internal_costs(legacy_data, api_client, settings):
    """Business default B-1: the public response omits ``cost_breakdown`` and ``totals``; every other key, in order,
    is byte-identical to the golden body (the service-level test above keeps the full body)."""
    from freezegun import freeze_time

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**rates, "public_write": "1000/min"}}
    golden = golden_cases()["cases"]
    sample = [case for case in golden if case["status"] == 200][::40] + [case for case in golden if case["status"] == 400]
    with freeze_time(f"{GOLDEN_DAY} 12:00:00"):
        for case in sample:
            response = api_client.post("/api/public/v1/bom/quote/", case["request"], format="json")
            if case["status"] == 200:
                assert response.status_code == 200 and not set(INTERNAL_KEYS) & set(response.json())
                expected = {key: value for key, value in case["response"].items() if key not in INTERNAL_KEYS}
                assert list(expected) == ["bom_lines", "pricing", "meta", "available_offers"]
                assert _dumped(json.loads(response.content)) == _dumped(expected), case["request"]
            else:
                assert response.status_code == 400 and response.json()["code"] == "validation_error"
                assert response.json()["message"] == "; ".join(case["response"]["errors"])
