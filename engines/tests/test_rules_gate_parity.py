"""Golden parity: the 8-check generation gate, the subsidy treatment derivation and the record → gate inputs."""

from __future__ import annotations

import pytest

from engines import gate
from engines.tests.rules_golden import assert_same, cases, frozen, ids, load

FILE = "rules_gate.json"


def test_the_eight_checks_in_order():
    assert load(FILE)["header"]["checks"] == [check.value for check in gate.GATE_CHECKS]
    assert len(gate.GATE_CHECKS) == 8


def _evaluate(data):
    return gate.evaluate_generation_gate(
        customer=frozen(data.get("customer")),
        system=frozen(data.get("system")),
        bom_snapshot=frozen(data.get("bomSnapshot")),
        engineering=frozen(data.get("engineering")),
        commercial_snapshot=frozen(data.get("commercialSnapshot")),
        subsidy_treatment=data.get("subsidyTreatment"),
        quotation=frozen(data.get("quotation")),
    )


@pytest.mark.parametrize("case", cases(FILE, "gate"), ids=ids(cases(FILE, "gate")))
def test_gate(case):
    report = _evaluate(case["input"])
    assert_same(case["output"], report)
    if case["check"] is None:
        assert report.passed
    elif case["check"] in {check.value for check in gate.GateCheck}:
        # each failing check fails alone
        assert [check.value for check in report.blocked_checks] == [case["check"]]


def test_each_check_has_a_case_failing_it_alone():
    alone = {case["check"] for case in cases(FILE, "gate") if case["check"] in {check.value for check in gate.GateCheck}}
    assert alone == {check.value for check in gate.GateCheck}


@pytest.mark.parametrize("case", cases(FILE, "treatment"), ids=ids(cases(FILE, "treatment")))
def test_subsidy_treatment(case):
    assert gate.derive_subsidy_treatment(frozen(case["input"]["record"]), frozen(case["input"]["subsidyResult"])) == case["output"]


@pytest.mark.parametrize("case", cases(FILE, "inputs"), ids=ids(cases(FILE, "inputs")))
def test_record_to_gate_inputs(case):
    data = case["input"]
    result = gate.resolve_inputs(frozen(data["record"]), data["version"], bom_snapshot=frozen(data["bomSnapshot"]), commercial_snapshot=frozen(data["commercialSnapshot"]))
    assert_same(case["output"], result)
