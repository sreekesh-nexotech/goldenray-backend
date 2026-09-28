"""engines.inspection_checks: the three versioned checklists and the full-definition status."""

from __future__ import annotations

import pytest

from engines import inspection_checks as ic

ON_GRID = ic.EquipmentType.ON_GRID_INVERTER
HYBRID_INVERTER = ic.EquipmentType.HYBRID_INVERTER
BATTERY = ic.EquipmentType.HYBRID_BATTERY


def answers(kind, value="PASS", **overrides):
    results = dict.fromkeys(ic.checklist(kind).ids, value)
    results.update(overrides)
    return results


def test_checklist_sizes_and_version():
    assert ic.CHECKS_VERSION == "v1"
    assert [len(ic.checklist(kind).checks) for kind in ic.EquipmentType] == [14, 16, 19]
    for kind in ic.EquipmentType:
        definition = ic.checklist(kind)
        assert definition.version == "v1" and definition.equipment_type == kind
        assert len(set(definition.ids)) == len(definition.ids)
        assert all(check.requires_evidence_on_failure for check in definition.checks)


def test_hybrid_inverter_is_the_on_grid_list_with_hybrid_cable_checks_at_the_end():
    on_grid = ic.checklist(ON_GRID)
    hybrid = ic.checklist(HYBRID_INVERTER)
    assert hybrid.ids[:13] == tuple(check_id for check_id in on_grid.ids if check_id != "cable_route")
    assert hybrid.ids[13:] == ("cable_route", "battery_distance", "battery_cable_length")
    assert hybrid.checks[13].description == "Safe route is available for PV, AC, battery and communication cables."
    assert on_grid.checks[10].description == "Safe and practical route is available for required cables."


def test_checklist_as_dict():
    data = ic.checklist(BATTERY).as_dict()
    assert data["equipment_type"] == "HYBRID_BATTERY" and data["checks_version"] == "v1" and data["title"] == "Hybrid Battery"
    assert data["checks"][0] == {"id": "safe_location", "label": "Safe Location", "description": "Battery can be installed in a secure and controlled area.", "requires_evidence_on_failure": True}


def test_unknown_type_or_version():
    with pytest.raises(ValueError):
        ic.checklist("SOLAR_PANEL")
    with pytest.raises(ValueError):
        ic.checklist(ON_GRID, "v9")


def test_required_equipment_types():
    assert ic.required_equipment_types("ON_GRID") == (ON_GRID,)
    assert ic.required_equipment_types("HYBRID") == (HYBRID_INVERTER, BATTERY)
    assert ic.required_equipment_types("UNDECIDED") == ()
    with pytest.raises(ValueError):
        ic.required_equipment_types("hybrid")


@pytest.mark.parametrize(
    "results,status",
    [
        ({}, "NOT_STARTED"),
        (None, "NOT_STARTED"),
        ({"direct_sunlight": "NOT_CHECKED"}, "NOT_STARTED"),
        ({"direct_sunlight": "PASS"}, "IN_PROGRESS"),  # legacy: PASS
        ({"direct_sunlight": "NOT_APPLICABLE"}, "IN_PROGRESS"),  # legacy: PASS
        ({"direct_sunlight": "FAIL"}, "FAIL"),
        ({"direct_sunlight": "NEEDS_REVIEW"}, "REQUIRES_REVIEW"),
        ({"direct_sunlight": "FAIL", "height": "NEEDS_REVIEW"}, "FAIL"),
        ("all_pass", "PASS"),
        ("all_na", "PASS"),
        ("one_na", "PASS"),
        ("one_missing", "IN_PROGRESS"),
        ("one_not_checked", "IN_PROGRESS"),
        ("one_fail", "FAIL"),
        ("one_review", "REQUIRES_REVIEW"),
    ],
)
def test_compute_status_over_the_full_definition(results, status):
    presets = {
        "all_pass": answers(ON_GRID),
        "all_na": answers(ON_GRID, "NOT_APPLICABLE"),
        "one_na": answers(ON_GRID, height="NOT_APPLICABLE"),
        "one_missing": {k: v for k, v in answers(ON_GRID).items() if k != "height"},
        "one_not_checked": answers(ON_GRID, height="NOT_CHECKED"),
        "one_fail": answers(ON_GRID, height="FAIL"),
        "one_review": answers(ON_GRID, height="NEEDS_REVIEW"),
    }
    assert ic.compute_status(ON_GRID, presets.get(results, results) if isinstance(results, str) else results) == status


def test_a_battery_checklist_is_scored_against_its_own_definition():
    assert ic.compute_status(BATTERY, answers(BATTERY)) == "PASS"
    with pytest.raises(ValueError):
        ic.compute_status(BATTERY, answers(ON_GRID))  # inverter check ids are not battery checks


def test_validate_results():
    assert ic.validate_results(ON_GRID, answers(ON_GRID)) == {}
    errors = ic.validate_results(ON_GRID, {"direct_sunlight": "OK", "battery_distance": "PASS", 7: "PASS", "height": None})
    assert set(errors) == {"direct_sunlight", "battery_distance", "7", "height"}
    assert ic.validate_results(ON_GRID, ["PASS"]) == {"results": "Must be an object of check id → result."}
    assert ic.validate_results(ON_GRID, {"direct_sunlight": ["PASS"]}) != {}


def test_normalise_results_fills_every_check_in_definition_order():
    normalised = ic.normalise_results(HYBRID_INVERTER, {"battery_cable_length": "FAIL"})
    assert list(normalised) == list(ic.checklist(HYBRID_INVERTER).ids)
    assert normalised["battery_cable_length"] == ic.CheckResult.FAIL
    assert sum(1 for value in normalised.values() if value == ic.CheckResult.NOT_CHECKED) == 15
    with pytest.raises(ValueError):
        ic.normalise_results(HYBRID_INVERTER, {"nope": "PASS"})


def test_result_counts():
    counts = ic.result_counts(ON_GRID, {"direct_sunlight": "FAIL", "height": "NOT_APPLICABLE"})
    assert counts == {"NOT_CHECKED": 12, "PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 0, "NOT_APPLICABLE": 1}
    assert ic.RESULT_LABEL[ic.CheckResult.NOT_APPLICABLE] == "N/A"


def test_resolution_rules():
    assert ic.is_resolved("PASS", None)
    assert ic.is_resolved("FAIL", "WAIVED") and ic.is_resolved("REQUIRES_REVIEW", "RESOLVED")
    assert not ic.is_resolved("FAIL", "PENDING") and not ic.is_resolved("FAIL", "NOT_REQUIRED")
    assert not ic.is_resolved("IN_PROGRESS", "WAIVED")  # a waiver never completes an unanswered checklist
    assert ic.is_critical("FAIL") and ic.is_critical("REQUIRES_REVIEW") and not ic.is_critical("PASS")
    assert ic.is_complete("PASS") and ic.is_complete("FAIL") and not ic.is_complete("IN_PROGRESS") and not ic.is_complete("NOT_STARTED")
    assert ic.evidence_required("FAIL") and not ic.evidence_required("REQUIRES_REVIEW")


def test_unanswered_checks_are_read_from_the_answers_not_the_status():
    """A FAIL/REQUIRES_REVIEW status says nothing about the other checks: one FAIL tap leaves 13 checks unanswered."""
    one_fail = {"direct_sunlight": "FAIL"}
    assert ic.compute_status(ON_GRID, one_fail) == "FAIL" and ic.is_critical("FAIL")
    assert len(ic.unanswered_checks(ON_GRID, one_fail)) == 13
    assert "direct_sunlight" not in ic.unanswered_checks(ON_GRID, one_fail)
    assert ic.unanswered_checks(ON_GRID, answers(ON_GRID, height="NOT_CHECKED")) == ("height",)
    assert ic.unanswered_checks(ON_GRID, answers(ON_GRID, height="FAIL")) == ()
    assert ic.unanswered_checks(BATTERY, None) == ic.checklist(BATTERY).ids
    with pytest.raises(ValueError):
        ic.unanswered_checks(ON_GRID, {"nope": "PASS"})


def test_legacy_status_edge_cases():
    assert ic.legacy_status({}) == "NOT_STARTED"
    assert ic.legacy_status({"a": "PASS", "b": "NOT_APPLICABLE"}) == "PASS"
    assert ic.legacy_status({"a": "PASS", "b": "WHATEVER"}) == "CONDITIONAL"
