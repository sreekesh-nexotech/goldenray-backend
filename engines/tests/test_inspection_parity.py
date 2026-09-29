"""Parity with legacy Site Inspection V2: identical answers except for the documented defects.

``golden/si_legacy.json`` holds the real legacy ``getReadiness`` / ``getFieldCompletion`` blockers for every scenario of
``inspection_cases.py``, the legacy ``calculateAssessmentStatus`` for a set of answer maps and the legacy checklist
definitions (captured by ``scripts/parity/capture_si_legacy.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engines import inspection_checks as ic
from engines import inspection_readiness as ir
from engines.tests import inspection_cases as cases

GOLDEN = json.loads((Path(__file__).parent / "golden" / "si_legacy.json").read_text(encoding="utf-8"))


def legacy_codes(blockers: list[str]) -> list[str]:
    return [cases.LEGACY_TEXT[text] for text in blockers]


def collapse(codes) -> list[str]:
    out: list[str] = []
    for code in codes:
        value = cases.DIMENSION_COLLAPSE.get(str(code), str(code))
        if value not in out:
            out.append(value)
    return out


def test_golden_covers_every_scenario_and_result_set():
    ids = [scenario.id for scenario in cases.SCENARIOS]
    assert len(ids) == len(set(ids))
    assert set(GOLDEN["readiness"]) == set(ids) == set(GOLDEN["completion"])
    assert set(GOLDEN["statuses"]) == set(cases.RESULT_SETS)
    assert set(GOLDEN["source"]) == {"lib/site-inspection.ts", "lib/equipment-assessment.ts"}


def test_every_legacy_blocker_text_is_mapped():
    texts = {text for part in ("readiness", "completion") for result in GOLDEN[part].values() for text in result["blockers"]}
    assert texts <= set(cases.LEGACY_TEXT)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda scenario: scenario.id)
def test_readiness_matches_legacy_except_documented_fixes(scenario):
    result = ir.evaluate(cases.to_state(scenario))
    assert result.codes == tuple(scenario.ready)
    assert result.ready is (not scenario.ready)
    legacy = GOLDEN["readiness"][scenario.id]
    if scenario.readiness_fix:
        assert legacy_codes(legacy["blockers"]) != [str(code) for code in result.codes], f"{scenario.id}: legacy already agrees — drop the fix note"
    else:
        assert legacy_codes(legacy["blockers"]) == [str(code) for code in result.codes]
        assert legacy["ready"] is result.ready


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda scenario: scenario.id)
def test_field_completion_matches_legacy_except_documented_fixes(scenario):
    result = ir.field_completion(cases.to_state(scenario))
    assert result.codes == tuple(scenario.completion)
    legacy = GOLDEN["completion"][scenario.id]
    if scenario.completion_fix:
        assert collapse(legacy_codes(legacy["blockers"])) != collapse(result.codes)
    else:
        assert collapse(legacy_codes(legacy["blockers"])) == collapse(result.codes)
        assert legacy["ready"] is result.ready


def test_every_documented_fix_names_its_source():
    notes = [note for scenario in cases.SCENARIOS for note in (scenario.readiness_fix, scenario.completion_fix) if note]
    assert notes and all(note.startswith(("§J", "§C", "Plan 2", "PLAN")) for note in notes)
    fixed = " ".join(notes)
    for defect in ("§J 7", "§J 13", "§J 16", "§J 17", "§J 18", "§J 19", "§J 20", "§J 22", "§J 30"):
        assert defect in fixed, f"no scenario demonstrates {defect}"


def test_checklists_are_the_legacy_definitions_word_for_word():
    legacy = {definition["type"]: definition for definition in GOLDEN["checklists"]}
    assert list(legacy) == [kind.value for kind in ic.EquipmentType]
    for kind in ic.EquipmentType:
        ours = ic.checklist(kind, "v1")
        theirs = legacy[kind.value]
        assert (ours.title, ours.description) == (theirs["title"], theirs["description"])
        assert [(c.id, c.label, c.description, c.requires_evidence_on_failure) for c in ours.checks] == [
            (c["id"], c["label"], c["description"], c["requiresEvidenceOnFailure"]) for c in theirs["checks"]
        ]
    assert [len(legacy[kind.value]["checks"]) for kind in ic.EquipmentType] == [14, 16, 19]


@pytest.mark.parametrize("name", sorted(cases.RESULT_SETS))
def test_legacy_status_port_is_exact(name):
    assert ic.legacy_status(cases.RESULT_SETS[name]) == GOLDEN["statuses"][name]


#: Answer maps whose status the full-definition rule changes (legacy scored the keys present only).
FULL_DEFINITION_CHANGES = {
    "one_pass": ("PASS", "IN_PROGRESS"),
    "one_na": ("PASS", "IN_PROGRESS"),
}


@pytest.mark.parametrize("name", sorted(set(cases.RESULT_SETS) - {"unknown_value"}))
def test_full_definition_status_differs_only_on_partial_answers(name):
    ours = ic.compute_status("ON_GRID_INVERTER", cases.RESULT_SETS[name])
    legacy = GOLDEN["statuses"][name]
    if name in FULL_DEFINITION_CHANGES:
        assert (legacy, ours) == FULL_DEFINITION_CHANGES[name]
    else:
        assert ours == legacy


def test_unknown_answers_are_rejected_rather_than_scored_conditional():
    assert GOLDEN["statuses"]["unknown_value"] == "CONDITIONAL"
    with pytest.raises(ValueError):
        ic.compute_status("ON_GRID_INVERTER", cases.RESULT_SETS["unknown_value"])
