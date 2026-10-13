"""Hold the platform BOM engine to Golden-Ray-BOM, the behavioural source of truth.

The corpus under ``parity/source/`` was captured from the tool's own JavaScript (see
``parity/capture_source.mjs``). A difference here is a platform defect unless it is in
``support.APPROVED_DIFFERENCES``.

**This suite is expected to fail until the port lands.** It is the gate the phased work
burns down, not a regression net around current behaviour: the platform still prices with
per-line GST buckets and takes margin on a GST-inclusive subtotal, while the source uses
a flat composite rate, puts flat-roof structure in the material lines, and takes margin
on pre-GST cost. ``test_burndown`` prints the standing count so progress is visible
without reading 1,217 assertions.

Marked ``source_parity`` so it can be deselected while a phase is in flight:

    pytest bom/tests/test_source_parity.py -v           # the gate
    pytest bom/ -m "not source_parity"                  # everything else
    pytest bom/tests/test_source_parity.py -k standard  # one phase's slice
"""

from __future__ import annotations

from datetime import date

import pytest

from bom.services.website_quote import quote
from bom.tests.parity import support, translate

pytestmark = [pytest.mark.django_db, pytest.mark.source_parity]

#: The corpus was captured against this catalog, so dates that affect offers are pinned
#: to the capture day — an offer expiring between capture and replay would otherwise show
#: up as a pricing difference.
TODAY = date(2026, 10, 13)


@pytest.fixture(scope="module")
def manifest():
    return support.manifest()


@pytest.fixture
def flarize_world(db):
    """Load the captured ``catalog.json`` through the three Flarize importers.

    These are the platform's own importers, not test fixtures, so the data under test
    arrives the way production data would. Market-rate keys the importer cannot parse are
    returned as violations and asserted on in :class:`TestCorpusIntegrity` rather than
    being quietly tolerated here.
    """
    from bom.services import legacy_import as bom_import
    from catalog.services import legacy_import as catalog_import
    from pricing.models import MarketRateSet
    from pricing.services import legacy_import as pricing_import
    from pricing.services.market_rates import activate_set

    catalog_json = support.catalog()
    results = {"catalog": catalog_import.import_flarize_catalog(catalog_json, None)}
    # The catalog importer RETURNS component prices as data; it does not write them.
    # They have to be handed to the pricing package, which owns `Price` — the same
    # hand-off bom/tests/legacy_fixtures.py:70 makes. Without it every component is
    # ACTIVE but unpriced, and `_items()` drops it ("nothing to quote without a current
    # LIST price"), so every variable slot silently vanishes from the BOM.
    results["prices"] = pricing_import.import_prices(results["catalog"]["prices"])
    results["pricing"] = pricing_import.import_flarize_pricing(catalog_json)
    results["bom"] = bom_import.import_flarize_bom(catalog_json)
    for rate_set in MarketRateSet.objects.exclude(status="ACTIVE"):
        activate_set(rate_set, user=None)
    # Golden-Ray-BOM prices with the composite regime, so the gate has to run the engine
    # the same way. The key defaults to PER_LINE precisely so a deployment cannot flip
    # the public website's prices; a test asserting source parity has to opt in.
    _set_composite_gst()
    return results


def _set_composite_gst():
    """Switch the engine to the composite regime the source prices with, at its rate.

    ``rate_pct: 9`` is stated outright rather than derived from the 70/30 split, which
    only approximates it (8.9 %). Golden Ray invoices solar at a flat 9 %, so that is the
    rate a quote must show — and stating it is what lets this gate demand exact parity
    instead of tolerating a per-case delta.
    """
    from pricing.services.cost_config import put_values

    put_values(user=None, entries=[{"key": "gst.quote_regime", "value": {"mode": "COMPOSITE", "rate_pct": 9}}])


def _case_ids(cases):
    return [case["id"] for case in cases]


# ── integrity of the corpus itself ────────────────────────────────────────────────────


class TestCorpusIntegrity:
    """Guard the corpus before trusting any comparison made against it."""

    def test_manifest_matches_the_files(self, manifest):
        total = 0
        for key in support.CORPORA:
            cases = support.load_corpus(key)
            assert cases, f"corpus_{key} is empty"
            assert manifest["corpora"][f"corpus_{key}"] == len(cases)
            total += len(cases)
        assert total == manifest["total_cases"]

    def test_every_case_is_self_describing(self):
        """A failing case must be reproducible from the file alone."""
        for key, engines in support.CORPORA.items():
            seen_ids = set()
            for case in support.load_corpus(key):
                assert case["id"], f"corpus_{key}: a case has no id"
                assert case["id"] not in seen_ids, f"corpus_{key}: duplicate id {case['id']}"
                seen_ids.add(case["id"])
                assert case["engine"] in engines, f"{case['id']}: engine is {case['engine']}, expected one of {sorted(engines)}"
                assert isinstance(case["params"], dict), f"{case['id']}: params is not an object"
                assert "catalog" not in case["params"], f"{case['id']}: catalog should be pinned by sha, not inlined"

    def test_the_source_divergences_hold_across_the_corpus(self):
        """The three behaviours the port exists to adopt, asserted on the captured data.

        If one of these ever fails it means the corpus was re-captured from a source that
        no longer behaves this way — which invalidates the plan, not just the test.
        """
        cases = support.load_corpus("standard")
        for case in cases:
            result, params = case["result"], case["params"]
            totals = result["totals"]
            assert round(totals["matTotal"] * 9 / 100) == totals["totalGst"], f"{case['id']}: not flat 9% GST"
            assert any(line.get("isStructure") for line in result["lines"]), f"{case['id']}: no structure material lines"
            if params.get("marginType", "percent") == "percent":
                expected = round(result["preGstSub"] * params.get("marginVal", 20) / 100)
                assert result["mAmt"] == expected, f"{case['id']}: margin not taken on pre-GST cost"

    def test_the_market_rate_keys_the_importer_cannot_parse(self, flarize_world):
        """Phase 3's measurement, pinned as a test.

        ``MARKET_KEY_RE`` matches ``variant`` only after ``_up``, so a key whose DCR
        marker precedes the future size cannot parse. Twelve live keys are lost this way,
        four of them carrying real prices. This asserts the loss so the number cannot
        drift unnoticed, and FAILS once Phase 3 fixes the regex — at which point the
        expectation becomes zero and this test documents the fix.
        """
        violations = [
            violation
            for violation in flarize_world["pricing"]["violations"]
            if violation.get("code") == "unknown_market_rate_key"
        ]
        lost = sorted(violation["source_id"] for violation in violations)
        assert lost == [
            "hybrid_value_0_dcr_diffBase",
            "hybrid_value_0_dcr_up5",
            "hybrid_value_0_dcr_up5_diffBase",
            "hybrid_value_0_ndcr_diffBase",
            "hybrid_value_0_ndcr_up5",
            "hybrid_value_0_ndcr_up5_diffBase",
            "hybrid_value_1_dcr_diffBase",
            "hybrid_value_1_dcr_up5",
            "hybrid_value_1_dcr_up5_diffBase",
            "hybrid_value_1_ndcr_diffBase",
            "hybrid_value_1_ndcr_up5",
            "hybrid_value_1_ndcr_up5_diffBase",
        ], "the set of unparseable market-rate keys changed"

    def test_the_importers_load_the_catalog_without_error(self, flarize_world):
        """Everything except the known market-rate losses must import cleanly."""
        for name in ("catalog", "bom"):
            errors = [v for v in flarize_world[name]["violations"] if v.get("severity") == "error"]
            assert not errors, f"{name} import reported errors: {errors[:3]}"
        pricing_errors = [
            v
            for v in flarize_world["pricing"]["violations"]
            if v.get("severity") == "error" and v.get("code") != "unknown_market_rate_key"
        ]
        assert not pricing_errors, f"pricing import reported unexpected errors: {pricing_errors[:3]}"


# ── the gate ──────────────────────────────────────────────────────────────────────────


def _compare(case, request_body, *, projections):
    """Run one case through the platform and return its differing leaves."""
    body = quote(request_body, today=TODAY)
    problems = []
    for name, (of_source, of_platform) in projections.items():
        expected, actual = of_source(case["result"]), of_platform(body)
        for difference in support.differences(expected, actual):
            problems.append(f"{name}.{difference}")
    return problems


STANDARD_PROJECTIONS = {
    "totals": (translate.source_totals, translate.platform_totals),
    "pricing": (translate.source_pricing, translate.platform_pricing),
    "lines": (translate.source_lines, translate.platform_lines),
}


class TestStandardBom:
    @pytest.mark.parametrize("case", support.load_corpus("standard"), ids=_case_ids(support.load_corpus("standard")))
    def test_case(self, case, flarize_world):
        problems = _compare(case, translate.quote_request(case["params"]), projections=STANDARD_PROJECTIONS)
        assert not problems, f"{case['id']}: {len(problems)} differences\n" + "\n".join(f"  {p}" for p in problems[:25])


class TestFutureUpgrade:
    @pytest.mark.parametrize(
        "case", support.load_corpus("standard_fu"), ids=_case_ids(support.load_corpus("standard_fu"))
    )
    def test_case(self, case, flarize_world):
        problems = _compare(case, translate.quote_request(case["params"]), projections=STANDARD_PROJECTIONS)
        assert not problems, f"{case['id']}: {len(problems)} differences\n" + "\n".join(f"  {p}" for p in problems[:25])


class TestPremiumMicro:
    @pytest.mark.parametrize("case", support.load_corpus("micro"), ids=_case_ids(support.load_corpus("micro")))
    def test_case(self, case, flarize_world):
        problems = _compare(case, translate.quote_request(case["params"]), projections=STANDARD_PROJECTIONS)
        assert not problems, f"{case['id']}: {len(problems)} differences\n" + "\n".join(f"  {p}" for p in problems[:25])


class TestUpgradeBom:
    @pytest.mark.parametrize("case", support.load_corpus("upgrade"), ids=_case_ids(support.load_corpus("upgrade")))
    def test_case(self, case, flarize_world):
        problems = _compare(
            case, translate.upgrade_quote_request(case["params"]), projections=STANDARD_PROJECTIONS
        )
        assert not problems, f"{case['id']}: {len(problems)} differences\n" + "\n".join(f"  {p}" for p in problems[:25])


class TestDiscountAndSubsidy:
    @pytest.mark.parametrize("case", support.load_corpus("discount"), ids=_case_ids(support.load_corpus("discount")))
    def test_case(self, case, flarize_world):
        problems = _compare(
            case,
            translate.discount_request(case["params"]),
            projections={"pricing": (translate.source_subsidy, translate.platform_subsidy)},
        )
        assert not problems, f"{case['id']}: {len(problems)} differences\n" + "\n".join(f"  {p}" for p in problems[:25])


# ── progress ──────────────────────────────────────────────────────────────────────────


@pytest.mark.source_parity_summary
def test_burndown(flarize_world, capsys):
    """Print how many cases currently differ, per corpus.

    Always passes: this reports the gate's state, it does not gate. Phase 0 records the
    opening numbers; each later phase should shrink them. The string and bill corpora are
    skipped because they replay engines the platform does not have yet (Phases 2 and 4).
    """
    slices = {
        "standard": translate.quote_request,
        "standard_fu": translate.quote_request,
        "micro": translate.quote_request,
        "upgrade": translate.upgrade_quote_request,
    }
    rows = []
    for key, to_request in slices.items():
        cases = support.load_corpus(key)
        failed = 0
        for case in cases:
            try:
                if _compare(case, to_request(case["params"]), projections=STANDARD_PROJECTIONS):
                    failed += 1
            except Exception:  # noqa: BLE001 — an error is a difference for counting purposes
                failed += 1
        rows.append((key, failed, len(cases)))

    discount = support.load_corpus("discount")
    failed = 0
    for case in discount:
        try:
            if _compare(
                case,
                translate.discount_request(case["params"]),
                projections={"pricing": (translate.source_subsidy, translate.platform_subsidy)},
            ):
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1
    rows.append(("discount", failed, len(discount)))

    with capsys.disabled():
        print("\n  Golden-Ray-BOM parity burn-down")
        for key, failed, total in rows:
            print(f"    {key:14} {failed:5} / {total:5} differing")
        print(f"    {'TOTAL':14} {sum(r[1] for r in rows):5} / {sum(r[2] for r in rows):5}")
        print("    (string and bill corpora await Phases 2 and 4)")
