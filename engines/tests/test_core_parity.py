"""Golden parity of engines-core with the real Flarize JavaScript engines.

The golden files are written by ``engines/tests/golden/generate_core.mjs`` from the real modules (money.js,
energyEngine.js, savingsEngine.js, subsidyEngine.js, financeEngine.js, pricingEngine.js, and
quotationWorkspace.resolveFinanceResult). Every case is replayed and **every output field** compared as a decimal
string. The only differences allowed are

* D-8: cases with a ``'3P'``-style phase carry ``outputWithFix`` (the JavaScript run with ``'three'``), which the
  engines must produce with ``fix_three_phase_tariff=True``; ``output`` (the JavaScript quirk) with ``False``;
* the binary64 divergences pinned in ``golden/core_divergences.json`` — each one checked here to be what it claims.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from engines import energy, finance, money, savings, subsidy
from engines.money import js_round
from engines.tests import parity_runners as runners
from engines.tests.golden_support import GOLDEN, differences, load, text

FILES = tuple(runners.RUNNERS)
DIVERGENCES = load("core_divergences.json")["divergences"]
PINNED = {(d["file"], d["case"], d["variant"], d["path"], d["javascript"], d["python"]): d for d in DIVERGENCES}


def _replay_params():
    return [pytest.param(name, case, id=f"{name[5:-5]}:{case['id']}") for name in FILES for case in load(name)["cases"]]


def _pinned(name: str, case_id: str, variant: str) -> set[tuple[str, str, str]]:
    return {key[3:] for key in PINNED if key[:3] == (name, case_id, variant)}


# ---- the files themselves --------------------------------------------------------------------------------------------


def test_versions_match_the_javascript_version_strings():
    assert load("core_money.json")["header"]["version"] == money.MONEY_RULE_VERSION == "money.1"
    assert load("core_money.json")["header"]["constants"] == {
        "CURRENCY": money.CURRENCY,
        "ROUNDING_MODE": money.ROUNDING_MODE,
        "ROUNDING_UNIT": money.ROUNDING_UNIT,
        "MONEY_RULE_VERSION": money.MONEY_RULE_VERSION,
    }
    assert load("core_energy.json")["header"]["version"] == energy.ENERGY_ENGINE_VERSION == "energyEngine.2"
    assert load("core_energy_tables.json")["header"]["version"] == energy.ENERGY_ENGINE_VERSION
    assert load("core_savings.json")["header"]["version"] == savings.SAVINGS_ENGINE_VERSION == "2.0.0"
    assert load("core_subsidy.json")["header"]["version"] == subsidy.SUBSIDY_ENGINE_VERSION == "1.0.0"
    assert load("core_finance.json")["header"]["version"] == finance.FINANCE_ENGINE_VERSION == "1.1.0"


def test_the_grid_is_broad():
    counts = {name: load(name)["header"]["caseCount"] for name in FILES}
    assert all(counts[name] == len(load(name)["cases"]) for name in FILES)
    assert sum(counts.values()) >= 1500, counts
    assert counts["core_energy.json"] >= 700 and counts["core_savings.json"] >= 250 and counts["core_subsidy.json"] >= 300 and counts["core_finance.json"] >= 400
    energy_ids = {case["id"].split(":")[0] for case in load("core_energy.json")["cases"]}
    assert {"boundary", "monthly", "phase", "size", "decimal", "cycle", "config", "region", "tie", "half-rupee"} <= energy_ids
    d8 = [case for case in load("core_energy.json")["cases"] if "outputWithFix" in case]
    assert {case["input"]["phase"] for case in d8} == set(load("core_energy.json")["header"]["d8Phases"])
    assert any("outputWithFix" in case for case in load("core_savings.json")["cases"])
    subsidy_sizes = {case["input"].get("systemSizeKw") for case in load("core_subsidy.json")["cases"]}
    assert {1, 2, Decimal("2.5"), 3, 10} <= subsidy_sizes


# ---- every case, every field -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, case", _replay_params())
def test_case_matches_the_javascript(name, case):
    """Every field equal, except the divergences pinned for this case (and D-8 via ``outputWithFix``)."""
    variants = (
        [(runners.JAVASCRIPT, case["output"], False), (runners.D8, case.get("outputWithFix", case["output"]), True)] if name in runners.TWO_VARIANTS else [(runners.JAVASCRIPT, case["output"], None)]
    )
    runner = runners.RUNNERS[name]
    for variant, expected, fix in variants:
        actual = runner(case) if fix is None else runner(case, fix_three_phase_tariff=fix)
        observed = {difference.key for difference in differences(expected, actual)}
        pinned = _pinned(name, case["id"], variant)
        assert observed == pinned, f"{variant}: unexpected {sorted(observed - pinned)}; pinned but gone {sorted(pinned - observed)}"


def test_d8_cases_are_single_phase_without_the_fix_and_three_phase_with_it():
    """With the fix, a '3P' case equals the JavaScript run with 'three'; without it, the JavaScript's own '3P' run
    (billed as single phase)."""
    cases = [case for case in load("core_energy.json")["cases"] if "outputWithFix" in case]
    assert len(cases) >= 30
    for case in cases:
        if case["output"]["available"]:
            assert (case["output"]["phase"], case["outputWithFix"]["phase"]) == ("single", "three"), case["id"]
    assert any(case["output"].get("billBreakdown") != case["outputWithFix"].get("billBreakdown") for case in cases)


# ---- the pinned divergences are what they claim ----------------------------------------------------------------------


def test_every_pinned_divergence_is_classified():
    kinds = {entry["kind"] for entry in DIVERGENCES}
    assert kinds <= {"representation", "half", "consequence"}
    for entry in DIVERGENCES:
        if entry["kind"] == "consequence":
            cause = [other for other in DIVERGENCES if (other["file"], other["case"], other["variant"], other["path"]) == (entry["file"], entry["case"], entry["variant"], entry["because"])]
            assert cause and cause[0]["kind"] == "half", entry


@pytest.mark.parametrize("entry", [entry for entry in DIVERGENCES if entry["kind"] == "representation"], ids=lambda e: f"{e['case']}:{e['variant']}:{e['path']}")
def test_representation_divergence_is_binary64_noise(entry):
    javascript, python = Decimal(entry["javascript"]), Decimal(entry["python"])
    assert javascript != python
    assert abs(javascript - python) <= abs(python) * Decimal("1e-12")
    assert len(python.normalize().as_tuple().digits) <= 12, "the Python value is the short exact decimal"


def _case(name: str, case_id: str) -> dict:
    if name == "core_energy_tables.json":
        return dict(runners.units_to_bill_cases())[case_id]
    return next(case for case in load(name)["cases"] if case["id"] == case_id)


def _profile(name: str, case: dict, variant: str) -> energy.EnergyProfile:
    fix = variant == runners.D8
    if name == "core_energy.json":
        return energy.calculate_energy_profile(runners._energy_inputs(case["input"]), runners.energy_config(case["config"]), case.get("regionId"), fix_three_phase_tariff=fix)
    data = case["input"]
    return energy.calculate_energy_profile(runners._energy_inputs(data["energyInputs"]), runners.savings_energy_config(data["energyConfig"]), fix_three_phase_tariff=fix)


def _duty_exact_and_binary64(item: dict) -> tuple[Decimal, float, Decimal]:
    region = runners.energy_config(item.get("config", "real")).regions["kerala"]
    bill = energy.units_to_bill(item["units"], region, item["phase"])
    duty_pct = region.electricity_duty_pct
    binary_energy = energy._binary64_energy(float(item["units"]), energy._Binary64Region.of(region, item["phase"]))
    return bill.energy_charge_exact * duty_pct / 100, binary_energy * (float(duty_pct) / 100), Decimal(1)


def _exact_and_binary64(path: str, profile: energy.EnergyProfile) -> tuple[Decimal, float, Decimal]:
    """What ``Math.round`` is applied to — exactly, and as the JavaScript's binary64 evaluation — and the unit the
    rounded value is scaled back by (``Math.round(x × 100) / 100`` → 0.01)."""
    rate = profile.average_tariff_rate
    field = path.removeprefix("$.")
    if field == "averageTariffRate":
        total, units = profile.bill_breakdown.total, profile.monthly_consumption
        return total * 100 / (2 * units), ((float(total) / 2) / float(units)) * 100, Decimal("0.01")
    if field in ("monthlyKsebValueLow", "monthlyKsebValueHigh"):
        daily = profile.daily_generation_low if field.endswith("Low") else profile.daily_generation_high
        return daily * 30 * rate, float(daily) * 30 * float(rate), Decimal(1)
    consumption = profile.monthly_consumption
    generation = {
        "monthlySavings": profile.monthly_generation,
        "monthlySavingsLow": js_round(profile.daily_generation_low * 30),
        "monthlySavingsHigh": js_round(profile.daily_generation_high * 30),
    }[field]
    offset = min(generation, consumption)
    return offset * rate, float(offset) * float(rate), Decimal(1)


def _math_round(value: float) -> float:
    whole = math.floor(value)
    return whole + 1 if value - whole >= 0.5 else whole


@pytest.mark.parametrize("entry", [entry for entry in DIVERGENCES if entry["kind"] == "half"], ids=lambda e: f"{e['case']}:{e['variant']}:{e['path']}")
def test_half_divergence_is_an_exact_half_the_javascript_rounded_down(entry):
    """The exact value is k + ½; money.js rounds it up (Python); JavaScript's binary64 value sits just below k + ½."""
    case = _case(entry["file"], entry["case"])
    if entry["file"] == "core_energy_tables.json":
        exact, binary64, unit = _duty_exact_and_binary64(case)
    else:
        exact, binary64, unit = _exact_and_binary64(entry["path"], _profile(entry["file"], case, entry["variant"]))
    assert exact - int(exact) == Decimal("0.5"), f"{exact} is not a half"
    assert binary64 < float(exact), "the binary64 evaluation is below the half"
    assert Decimal(entry["python"]) == js_round(exact) * unit == Decimal(entry["javascript"]) + unit
    assert Decimal(_math_round(binary64)) * unit == Decimal(entry["javascript"])


# ---- dense tables: units → bill for every whole unit, bill → units for every whole bi-monthly rupee ---------------------


def _kerala() -> energy.RegionConfig:
    return runners.energy_config("real").regions["kerala"]


@pytest.mark.parametrize("phase", ["single", "three"])
def test_units_to_bill_for_every_whole_unit(phase):
    tables = load("core_energy_tables.json")
    region = _kerala()
    for units, row in enumerate(tables["unitsToBill"][phase]):
        bill = energy.units_to_bill(units, region, phase)
        assert [bill.energy_charge, bill.fixed_charge, bill.duty, bill.meter_rent, bill.total] == row, units


@pytest.mark.parametrize("case_id, item", runners.units_to_bill_cases(), ids=[case_id for case_id, _ in runners.units_to_bill_cases()])
def test_units_to_bill_fractional_units_and_config_shapes(case_id, item):
    observed = {difference.key for difference in differences(item["output"], runners.run_units_to_bill(item))}
    assert observed == _pinned("core_energy_tables.json", case_id, runners.JAVASCRIPT)


@pytest.mark.parametrize("phase", ["single", "three"])
def test_bill_to_units_for_every_whole_bi_monthly_rupee(phase):
    tables = load("core_energy_tables.json")
    region = _kerala()
    expected = tables["billToUnits"][phase]
    assert len(expected) == 30000
    mismatches = [(bill, units) for bill, units in enumerate(expected, start=1) if energy.bill_to_units(bill, region, phase) != units]
    assert not mismatches


@pytest.mark.parametrize("phase", ["single", "three"])
def test_bill_to_units_for_half_rupee_bills(phase):
    region = _kerala()
    expected = load("core_energy_tables.json")["billToUnitsHalfRupees"][phase]
    mismatches = [index for index, units in enumerate(expected) if energy.bill_to_units(Decimal(index) + Decimal("0.5"), region, phase) != units]
    assert not mismatches


def test_bill_to_units_under_other_config_shapes():
    for item in load("core_energy_tables.json")["billToUnitsVariants"]:
        region = runners.energy_config(item["config"]).regions["kerala"]
        assert energy.bill_to_units(item["bill"], region, item["phase"]) == item["units"], item


def _exact_bisection(target: Decimal, region: energy.RegionConfig, phase: str) -> int:
    """The same bisection in exact decimal arithmetic — what a naive Decimal port would do."""
    low, high = Decimal(0), Decimal(3000)
    for _ in range(50):
        middle = (low + high) / 2
        if energy.units_to_bill(middle, region, phase).total < target:
            low = middle
        else:
            high = middle
    return int(js_round((low + high) / 2))


def test_the_binary64_search_is_needed_for_parity():
    """At bills whose total jumps exactly at a half unit, an exact bisection lands on the other side of the half
    about half the time; the binary64 replica reproduces the JavaScript every time (module docstring)."""
    region = _kerala()
    ties = [case for case in load("core_energy.json")["cases"] if case["id"].startswith("tie:")]
    assert len(ties) >= 30
    exact_misses = 0
    for case in ties:
        bill, phase = case["input"]["billAmount"], case["input"]["phase"]
        assert energy.bill_to_units(bill, region, phase) == case["output"]["biMonthlyUnits"]
        exact_misses += _exact_bisection(Decimal(bill), region, phase) != case["output"]["biMonthlyUnits"]
    assert exact_misses > 0


# ---- the capture itself ----------------------------------------------------------------------------------------------

NODE = os.environ.get("NODE_BIN") or shutil.which("node") or ("/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None)
FLARIZE_ROOT = Path(os.environ.get("FLARIZE_ROOT", "/home/user/flarize-main/flarize/src/lib"))


@pytest.mark.skipif(NODE is None or not FLARIZE_ROOT.joinpath("energyEngine.js").exists(), reason="needs node and the Flarize sources (FLARIZE_ROOT)")
def test_golden_capture_is_reproducible(tmp_path):
    """Re-running the generator against the legacy sources reproduces the committed files byte for byte."""
    environment = {**os.environ, "GOLDEN_OUT": str(tmp_path), "FLARIZE_ROOT": str(FLARIZE_ROOT)}
    subprocess.run([NODE, "--no-warnings", str(GOLDEN / "generate_core.mjs")], check=True, capture_output=True, env=environment, timeout=300)
    for name in (*FILES, "core_energy_tables.json"):
        assert (tmp_path / name).read_bytes() == (GOLDEN / name).read_bytes(), name


def test_divergence_file_lists_values_as_the_comparison_prints_them():
    for entry in DIVERGENCES:
        assert entry["javascript"] == text(Decimal(entry["javascript"])) and entry["python"] == text(Decimal(entry["python"]))
    assert len({json.dumps(entry, sort_keys=True) for entry in DIVERGENCES}) == len(DIVERGENCES)
