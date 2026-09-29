"""Parity with the eSSL v3 engine: every difference is a documented A-row, and nothing else differs.

``golden/essl_v3_attendance.json`` is the real v3 engine's output (``scripts/parity/capture_essl_v3.py``) for every case
of ``attendance_cases.py``. For each case the fields v4 changes must be exactly the fields the case documents in ``v3``,
with exactly the v3 values quoted there, and a changed case must name the A-rows responsible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engines import attendance as att
from engines.tests import attendance_cases as cases
from engines.tests.attendance_support import comparable, day_rules, run_case, shift

GOLDEN = json.loads((Path(__file__).parent / "golden" / "essl_v3_attendance.json").read_text(encoding="utf-8"))
A_ROWS = {f"A{n}" for n in range(1, 13)}


def test_golden_was_captured_from_the_v3_engine():
    assert GOLDEN["source"]["processing_version"] == "v3-half-day-arrival"
    assert set(GOLDEN["cases"]) == {case.id for case in cases.CASES}
    assert set(GOLDEN["deadlines"]) == {case.id for case in cases.DEADLINES}
    assert set(GOLDEN["attributions"]) == {case.id for case in cases.ATTRIBUTIONS}
    assert len({case.id for case in cases.CASES}) == len(cases.CASES)


@pytest.mark.parametrize("case", cases.CASES, ids=lambda case: case.id)
def test_day_differs_from_v3_only_where_documented(case):
    v4 = run_case(case)
    v3 = GOLDEN["cases"][case.id]
    differing = {name for name in cases.COMPARED_FIELDS if comparable(getattr(v4, name)) != v3[name]}
    assert differing == set(case.v3), f"undocumented v3 → v4 change in {sorted(differing ^ set(case.v3))}: v3 {v3}"
    for name, value in case.v3.items():
        assert v3[name] == comparable(value), f"{case.id}: the v3 value quoted for {name} is not what v3 returns"
    assert set(case.changed_by) <= A_ROWS | {"C3"}
    if differing:
        assert set(case.changed_by) & A_ROWS, f"{case.id} changes {sorted(differing)} without naming an A-row"


@pytest.mark.parametrize("case", cases.DEADLINES, ids=lambda case: case.id)
def test_deadline_differs_from_v3_only_where_documented(case):
    the_shift = shift(case.shift)
    v4 = att.half_day_deadline(case.day, the_shift, day_rules(case.rules, the_shift, case.day))
    v3 = GOLDEN["deadlines"][case.id]
    assert comparable(v4) == comparable(case.expect)
    if case.v3 == "same":
        assert v3 == comparable(v4)
        assert not case.changed_by
    else:
        assert v3 == comparable(case.v3) != comparable(v4)
        assert case.changed_by == ("A5",)


@pytest.mark.parametrize("case", cases.ATTRIBUTIONS, ids=lambda case: case.id)
def test_attribution_differs_from_v3_only_where_documented(case):
    v4 = att.attribute_work_date(case.wall_clock, shift(case.shift))
    v3 = GOLDEN["attributions"][case.id]
    assert v4 == case.expect
    if case.v3 == "same":
        assert v3 == v4.isoformat()
        assert not case.changed_by
    else:
        assert v3 == case.v3.isoformat() != v4.isoformat()
        assert case.changed_by == ("A6",)


def test_break_deduction_differs_from_v3_exactly_on_the_discontinuity():
    gen = shift(cases.GEN)
    v4 = [att.deduct_break(gross, gen)[0] for gross in cases.BREAK_GROSS_RANGE]
    v3 = GOLDEN["break"]
    changed = {gross for gross in cases.BREAK_GROSS_RANGE if v4[gross] != v3[gross]}
    assert changed == cases.BREAK_CHANGED_GROSS
    # v3: 60 minutes → 60 worked, 61 → 1 (A2); v4 is continuous there.
    assert (v3[60], v3[61]) == (60, 1)
    assert (v4[60], v4[61]) == (60, 61)
