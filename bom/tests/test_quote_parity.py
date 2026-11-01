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
import re
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

#: The legacy engine inferred an upgrade's phase from its target size — ``to_kw >= 5``
#: meant three-phase — so a customer extending to 5 kW on a single-phase supply was quoted
#: three-phase parts: a 3P inverter, a 3P ACDB and a longer AC run. Golden-Ray-BOM takes
#: the phase as the sales person's choice, defaulting to single (App.jsx:695), and the port
#: follows it.
#:
#: All 30 of the golden file's upgrade cases were captured without ever sending a phase, so
#: every one of them encodes that inference. They are exempt here rather than re-captured:
#: this file is a historical record, and rewriting it to agree with a fix would destroy the
#: evidence of what the old engine did.
#:
#: The exemption is narrow by design. The 1,029 on-grid and hybrid cases — the ones that
#: price the public website — stay strict, which is the pin's actual job.
LEGACY_INFERRED_UPGRADE_PHASE = "upgrade"


def _dumped(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _inverters(body: dict) -> int:
    """How many actual inverters a body quotes.

    The response carries no field saying "this line is the inverter", and the inverter
    *section* also holds the DCDB and ACDB that go with it. Those two are identifiable by
    name wherever they appear — every catalog row for them says so — while the inverters
    themselves span brands, so excluding the boards is the stable way round.
    """
    return sum(
        1
        for line in body.get("bom_lines", [])
        if line.get("section") == "inverter" and not re.search(r"\bDCDB\b|\bACDB\b|AJ Box|Meter|Isolator", line["name"], re.IGNORECASE)
    )


#: Top-level keys the port adds. `string_config` is the installer's string/MPPT layout,
#: which the legacy engine never produced; it only appears under COMPOSITE, so it should
#: not reach this pin at all — stripping it is belt-and-braces against a regression that
#: un-gates it.
ADDED_BODY_KEYS = ("string_config",)


def _without_added_keys(body: dict) -> dict:
    """``body`` as the golden capture would have had it, before the port's new keys."""
    out = {k: v for k, v in body.items() if k not in ADDED_BODY_KEYS}
    if "totals" not in out:
        return out
    return {**out, "totals": {k: v for k, v in out["totals"].items() if k not in ADDED_TOTALS_KEYS}}


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
    mismatches, statuses, approved, exempt = [], {200: 0, 400: 0}, 0, 0
    for index, case in enumerate(golden["cases"]):
        if case["request"].get("sys_type") == LEGACY_INFERRED_UPGRADE_PHASE and "upgrade_phase" not in case["request"]:
            exempt += 1
            continue
        status, body = _replay(case)
        if case["status"] == 500:
            assert status == 400 and isinstance(case["request"].get("custom_discount"), (int, float)), case["request"]
            approved += 1
            continue
        statuses[case["status"]] += 1
        if status != case["status"] or _dumped(_without_added_keys(body)) != _dumped(case["response"]):
            mismatches.append((index, case["request"]))
    assert not mismatches, f"{len(mismatches)} differing cases, first: {mismatches[:3]}"
    # 1,029 on-grid/hybrid + 9 validation errors replayed strictly; 30 upgrade cases and
    # 9 legacy 500s accounted for. The counts are asserted so the exemption cannot quietly
    # widen: if it ever covers more than those 30, this fails.
    assert statuses == {200: 1029, 400: 9} and approved == 9 and exempt == 30


def test_the_exempt_upgrade_cases_differ_only_in_understood_ways(legacy_data):
    """Pin *why* the 30 exempt cases differ, so the exemption cannot hide a new defect.

    Four things changed on the upgrade path, all of them corrections to the legacy engine:

    1. phase is taken as an input rather than inferred from the target size;
    2. the DCDB is always the 2/2 part on an upgrade (the extra strings need the inputs);
    3. the two structure tube rows are quoted (the legacy engine skipped them on
       ``is_tube``, dropping ~₹2,289 of structure from every upgrade);
    4. a three-phase target adds the meter, meter box and isolator that changing the
       supply requires.

    Supplying the phase the legacy engine inferred isolates the first, and the rest show
    up as *added* lines and a *higher* material total — never as missing lines or a lower
    one, which is what this asserts. A regression that dropped parts would break it.
    """
    golden = [case for case in golden_cases()["cases"] if case["status"] == 200 and case["request"].get("sys_type") == "upgrade"]
    assert len(golden) == 30
    for case in golden:
        request = case["request"]
        to_kw = float(request.get("upgrade_to_kw", 5))
        inferred = "3P" if to_kw >= 5 else "1P"
        body = quote({**request, "upgrade_phase": inferred}, today=TODAY)

        # 1. With the inferred phase supplied, the phase agrees again -- unless a hybrid
        #    inverter is involved at 5 kW or below, where three-phase does not exist and
        #    the request is forced back. The legacy engine had no such rule and reported
        #    three-phase there regardless, which is itself one of the corrections.
        hybrid_forced = (request.get("upgrade_sections") or {}).get("hybrid_inv") and to_kw <= 5
        if hybrid_forced:
            assert body["meta"]["is_three_phase"] is False, request
        else:
            assert body["meta"]["is_three_phase"] == case["response"]["meta"]["is_three_phase"], request

        was = {line["name"] for line in case["response"]["bom_lines"]}
        now = {line["name"] for line in body["bom_lines"]}

        hybrid_inv = (request.get("upgrade_sections") or {}).get("hybrid_inv")

        if hybrid_inv:
            # The legacy engine honoured `inverter` and `hybrid_inv` at once and quoted
            # BOTH, so these bodies carry two inverters — billing the customer for one
            # they are having removed. Selecting a hybrid replaces the on-grid inverter
            # (App.jsx:1842-1850), so the correct answer has exactly one.
            #
            # Asserted structurally rather than by name: the golden bodies name thirteen
            # different inverters across brands (Sungrow, SolarEdge, Growatt, DEYE), so
            # matching on text would be a guess that silently stops catching things.
            assert _inverters(body) <= 1, f"{request}: {_inverters(body)} inverters quoted"
            assert _inverters(case["response"]) >= _inverters(body), request
        else:
            # 2/3/4. Nothing the legacy engine quoted has been dropped, except the 1/1
            # DCDB it should never have chosen for an upgrade and, where a hybrid forced
            # the phase back to single, the three-phase parts that cannot be fitted.
            allowed = ["DCDB", "3P"] if hybrid_forced else ["DCDB"]
            dropped = {name for name in was - now if not re.search("|".join(allowed), name)}
            assert not dropped, f"{request}: lines disappeared — {sorted(dropped)}"

        # The material total only ever goes up, by the parts the legacy engine omitted —
        # unless one of the over-quoted parts above has been removed, which makes it
        # rightly cheaper.
        if not hybrid_forced and not hybrid_inv:
            assert body["totals"]["material_ex_gst"] >= case["response"]["totals"]["material_ex_gst"], request


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
    # Same exemption as the strict replay above: the 30 upgrade cases encode the legacy
    # engine's phase inference, which the port deliberately no longer does.
    strict = [
        case
        for case in golden
        if not (case["request"].get("sys_type") == LEGACY_INFERRED_UPGRADE_PHASE and "upgrade_phase" not in case["request"])
    ]
    sample = [case for case in strict if case["status"] == 200][::40] + [case for case in strict if case["status"] == 400]
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
