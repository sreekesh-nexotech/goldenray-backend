"""engines.inspection_readiness: every blocker from a ready inspection, the PLAN §2.9 order, field completion, the
location snapshot, and the spec §C/§J fixes at the engine level."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from engines import inspection_readiness as ir
from engines.inspection_checks import checklist

C = ir.Code


def assessment(kind, value="PASS", review_status="NOT_REQUIRED", **overrides):
    results = dict.fromkeys(checklist(kind).ids, value)
    results.update(overrides)
    return ir.EquipmentAssessment(equipment_type=kind, results=results, review_status=review_status)


PANEL = ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}, width_m=Decimal("5.00"), height_m=Decimal("4.00"), area_m2=Decimal("20.00"))
EQUIPMENT = ir.Annotation("EQUIPMENT_AREA", "ph_equipment", {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3}, width_m=Decimal("1.50"), height_m=Decimal("2.00"), area_m2=Decimal("3.00"))


def ready_state(**changes) -> ir.InspectionState:
    state = ir.InspectionState(
        customer="cus_1",
        engineer="eng_1",
        visit_date=date(2026, 9, 1),
        address="Kaloor, Kochi",
        panel_photo="ph_panel",
        equipment_photo="ph_equipment",
        panel_width_m=Decimal("5.00"),
        panel_height_m=Decimal("4.00"),
        equipment_width_m=Decimal("1.50"),
        equipment_height_m=Decimal("2.00"),
        site_suitability="SUITABLE",
        complexity_status="ROUTINE",
        system_type="ON_GRID",
        neutral_link="AVAILABLE",
        termination_point="AVAILABLE",
        annotations=(PANEL, EQUIPMENT),
        approvals=(ir.Approval(1, "APPROVED", ir.build_location_snapshot((PANEL, EQUIPMENT))),),
        equipment=(assessment("ON_GRID_INVERTER"),),
    )
    return state.with_changes(**changes)


def test_the_ready_state_is_ready():
    result = ir.evaluate(ready_state())
    assert result.ready and result.blockers == () and all(result.checks.values())
    assert result.approved_location_number == 1 and result.suitability == "SUITABLE"
    assert ir.field_completion(ready_state()).ready


# ---------------------------------------------------------------------------------------------------------------
# the 19 checks, one blocker at a time
# ---------------------------------------------------------------------------------------------------------------

MOVED = ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.3, "y": 0.1, "w": 0.5, "h": 0.4}, width_m=5, height_m=4, area_m2=20)

SINGLE_BLOCKERS = [
    (C.CUSTOMER_REQUIRED, {"customer": None}),
    (C.ENGINEER_REQUIRED, {"engineer": ""}),
    (C.VISIT_DATE_REQUIRED, {"visit_date": None}),
    (C.PANEL_PHOTO_REQUIRED, {"panel_photo": None}),
    (C.EQUIPMENT_PHOTO_REQUIRED, {"equipment_photo": None}),
    (C.PANEL_WIDTH_REQUIRED, {"panel_width_m": 0}),
    (C.PANEL_HEIGHT_REQUIRED, {"panel_height_m": None}),
    (C.EQUIPMENT_WIDTH_REQUIRED, {"equipment_width_m": Decimal("-1")}),
    (C.EQUIPMENT_HEIGHT_REQUIRED, {"equipment_height_m": "NaN"}),
    (C.LOCATIONS_NOT_DOCUMENTED, {"annotations": (PANEL,), "approvals": (ir.Approval(1, "APPROVED", ir.build_location_snapshot((PANEL,))),)}),
    (C.RESTRICTIONS_NEED_REMARKS, {"has_location_restrictions": True, "customer_location_remarks": " "}),
    (C.SUITABILITY_UNCONFIRMED, {"site_suitability": None}),
    (C.SITE_NOT_SUITABLE, {"site_suitability": "NOT_SUITABLE"}),
    (C.SUITABILITY_CONDITIONAL, {"site_suitability": "CONDITIONAL"}),
    (C.CUSTOMER_APPROVAL_REQUIRED, {"approvals": ()}),
    (C.CUSTOMER_APPROVAL_OUTDATED, {"annotations": (MOVED, EQUIPMENT)}),
    (C.ENGINEERING_REVIEW_PENDING, {"engineering_reviews": (ir.EngineeringReview("PENDING"),)}),
    (C.ADDITIONAL_WORK_REJECTED, {"additional_work": (ir.AdditionalWork("WALKWAY", status="REJECTED"),)}),
    (C.ADDITIONAL_WORK_UNAPPROVED, {"additional_work": (ir.AdditionalWork("WALKWAY", customer_impacting=True, status="CUSTOMER_QUOTE_SENT"),)}),
    (C.EQUIPMENT_SYSTEM_TYPE_UNDECIDED, {"system_type": "UNDECIDED"}),
    (C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE, {"equipment": ()}),
    (C.EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION, {"equipment": (assessment("ON_GRID_INVERTER", clearance="FAIL"),)}),
    (C.EQUIPMENT_HYBRID_INVERTER_INCOMPLETE, {"system_type": "HYBRID", "equipment": (assessment("HYBRID_BATTERY"),)}),
    (
        C.EQUIPMENT_HYBRID_INVERTER_NEEDS_RESOLUTION,
        {"system_type": "HYBRID", "equipment": (assessment("HYBRID_INVERTER", battery_distance="NEEDS_REVIEW", review_status="PENDING"), assessment("HYBRID_BATTERY"))},
    ),
    (C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE, {"system_type": "HYBRID", "equipment": (assessment("HYBRID_INVERTER"), assessment("HYBRID_BATTERY", "NOT_CHECKED"))}),
    (C.EQUIPMENT_HYBRID_BATTERY_NEEDS_RESOLUTION, {"system_type": "HYBRID", "equipment": (assessment("HYBRID_INVERTER"), assessment("HYBRID_BATTERY", flooding="FAIL"))}),
    (C.WHEELING_CONSUMER_NUMBER_REQUIRED, {"wheeling_required": True, "consumer_number": None, "registered_phone_e164": "+919000000001"}),
    (C.WHEELING_PHONE_REQUIRED, {"wheeling_required": True, "consumer_number": "1100000000001", "registered_phone_e164": ""}),
    (C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED, {"neutral_link": "NOT_AVAILABLE"}),
    (C.LATEST_APPROVAL_NOT_APPROVED, {"approvals": (ir.Approval(1, "APPROVED", ir.build_location_snapshot((PANEL, EQUIPMENT))), ir.Approval(2, "REJECTED"))}),
]


@pytest.mark.parametrize("code,changes", SINGLE_BLOCKERS, ids=[str(code) for code, _ in SINGLE_BLOCKERS])
def test_each_blocker_alone(code, changes):
    result = ir.evaluate(ready_state(**changes))
    assert result.codes == (code,)
    assert not result.ready
    blocker = result.blockers[0]
    assert blocker.text == ir.BLOCKER_TEXT[code] and blocker.step == ir.STEP_OF[code]


def test_every_readiness_code_has_a_single_blocker_test():
    readiness_codes = {code for codes in ir.STEPS for code in codes}
    assert readiness_codes == {code for code, _ in SINGLE_BLOCKERS}


def test_nineteen_steps_in_the_plan_order():
    assert len(ir.STEPS) == 19
    assert [codes[0] for codes in ir.STEPS] == [
        C.CUSTOMER_REQUIRED,
        C.ENGINEER_REQUIRED,
        C.VISIT_DATE_REQUIRED,
        C.PANEL_PHOTO_REQUIRED,
        C.EQUIPMENT_PHOTO_REQUIRED,
        C.PANEL_WIDTH_REQUIRED,
        C.PANEL_HEIGHT_REQUIRED,
        C.EQUIPMENT_WIDTH_REQUIRED,
        C.EQUIPMENT_HEIGHT_REQUIRED,
        C.LOCATIONS_NOT_DOCUMENTED,
        C.RESTRICTIONS_NEED_REMARKS,
        C.SUITABILITY_UNCONFIRMED,
        C.CUSTOMER_APPROVAL_REQUIRED,
        C.ENGINEERING_REVIEW_PENDING,
        C.ADDITIONAL_WORK_REJECTED,
        C.EQUIPMENT_SYSTEM_TYPE_UNDECIDED,
        C.WHEELING_CONSUMER_NUMBER_REQUIRED,
        C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,
        C.LATEST_APPROVAL_NOT_APPROVED,
    ]


def test_every_code_has_text():
    assert set(ir.BLOCKER_TEXT) == set(C)
    assert ir.BLOCKER_TEXT[C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE] == "HYBRID BATTERY assessment is incomplete"


def test_blockers_come_out_in_step_order():
    everything = ir.InspectionState(
        has_location_restrictions=True,
        wheeling_required=True,
        system_type="HYBRID",
        complexity_status="ENGINEERING_REVIEW_REQUIRED",
        neutral_link="NEEDS_MODIFICATION",
        termination_point="NOT_AVAILABLE",
        approvals=(ir.Approval(1, "APPROVED"), ir.Approval(2, "PENDING")),
        additional_work=(ir.AdditionalWork("CIVIL_WORK", status="REJECTED"), ir.AdditionalWork("WALKWAY", customer_impacting=True)),
    )
    result = ir.evaluate(everything)
    steps = [blocker.step for blocker in result.blockers]
    assert steps == sorted(steps)
    assert len(set(steps)) == 19
    electrical = next(b for b in result.blockers if b.code == C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED)
    assert electrical.details == {"fields": ["neutral_link", "termination_point"]}
    assert {C.EQUIPMENT_HYBRID_INVERTER_INCOMPLETE, C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE} <= set(result.codes)
    assert result.approved_location_number == 1 and C.CUSTOMER_APPROVAL_OUTDATED in result.codes  # approved without a snapshot


def test_readiness_as_dict():
    data = ir.evaluate(ready_state(customer=None)).as_dict()
    assert data["ready"] is False
    assert data["blockers"] == [{"code": "CUSTOMER_REQUIRED", "text": "Customer is required", "step": 1, "details": {}}]
    assert data["checks"]["inspection_complete"] is False and data["suitability"] == "SUITABLE" and data["warnings"] == []


# ---------------------------------------------------------------------------------------------------------------
# spec §C/§J fixes at the engine level
# ---------------------------------------------------------------------------------------------------------------


def test_has_value_zero_is_a_value():
    assert ir.has_value(0) and ir.has_value(0.0) and ir.has_value(Decimal("0")) and ir.has_value(False)
    assert not ir.has_value(None) and not ir.has_value("  ") and not ir.has_value(float("nan")) and not ir.has_value(Decimal("NaN"))
    assert ir.evaluate(ready_state(wheeling_required=True, consumer_number=0, registered_phone_e164="+919000000001")).ready


def test_measurements_must_be_positive_numbers():
    assert ir.is_positive(1) and ir.is_positive("0.5") and ir.is_positive(Decimal("0.01"))
    for value in (0, -1, None, True, "abc", "", float("inf"), float("nan"), Decimal("sNaN")):
        assert not ir.is_positive(value), value


def test_suitability_normalisation_for_legacy_values():
    assert ir.normalise_suitability("CONDITIONAL", 0) == "CONDITIONAL"  # the recommendation wins over the 1/0 flag
    assert ir.normalise_suitability(None, 1) == "SUITABLE"
    assert ir.normalise_suitability(None, True) == "SUITABLE"
    assert ir.normalise_suitability(None, 0) == "NOT_SUITABLE"
    assert ir.normalise_suitability(None, False) == "NOT_SUITABLE"
    assert ir.normalise_suitability("conditionally suitable") == "CONDITIONAL"
    assert ir.normalise_suitability(" yes ") == "SUITABLE"
    assert ir.normalise_suitability("not suitable") == "NOT_SUITABLE"
    assert ir.normalise_suitability("MAYBE", "PERHAPS") is None  # unknown → unconfirmed, never a pass
    assert ir.normalise_suitability("", None) is None
    assert ir.normalise_suitability(None, 2) is None


def test_availability_normalisation():
    assert ir.normalise_availability("not available") == "NOT_AVAILABLE"
    assert ir.normalise_availability("Needs-Modification") == "NEEDS_MODIFICATION"
    assert ir.normalise_availability("AVAILABLE") == "AVAILABLE"
    assert ir.normalise_availability(" ") is None and ir.normalise_availability(None) is None
    with pytest.raises(ValueError):
        ir.normalise_availability("broken")


def test_neutral_link_and_termination_each_need_their_own_decision():
    assert ir.evaluate(ready_state(neutral_link="NOT_AVAILABLE", additional_work=(ir.AdditionalWork("NEW_NEUTRAL_LINK", status="IDENTIFIED"),))).ready
    assert ir.evaluate(ready_state(termination_point="NEEDS_MODIFICATION", additional_work=(ir.AdditionalWork("new_termination"),))).ready
    wrong_fix = ir.evaluate(ready_state(neutral_link="NOT_AVAILABLE", additional_work=(ir.AdditionalWork("NEW_TERMINATION"),)))
    assert wrong_fix.codes == (C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,)
    assert wrong_fix.blockers[0].details == {"fields": ["neutral_link"]}
    not_required = ir.evaluate(ready_state(neutral_link="NOT_AVAILABLE", additional_work=(ir.AdditionalWork("NEW_NEUTRAL_LINK", required=False),)))
    assert not_required.codes == (C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,)
    assert ir.evaluate(ready_state(neutral_link=None, termination_point=None)).ready  # not recorded: nothing to decide yet


def test_equipment_status_is_recomputed_from_the_answers():
    one_tap = ir.EquipmentAssessment("ON_GRID_INVERTER", results={"direct_sunlight": "PASS"})
    assert one_tap.status == "IN_PROGRESS"
    result = ir.evaluate(ready_state(equipment=(one_tap,)))
    assert result.codes == (C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE,)
    assert result.blockers[0].details == {"status": "IN_PROGRESS"}


def test_waived_or_resolved_reviews_clear_a_critical_assessment():
    for review in ("WAIVED", "RESOLVED"):
        assert ir.evaluate(ready_state(equipment=(assessment("ON_GRID_INVERTER", review_status=review, height="FAIL"),))).ready
    pending = ir.evaluate(ready_state(equipment=(assessment("ON_GRID_INVERTER", review_status="PENDING", height="NEEDS_REVIEW"),)))
    assert pending.codes == (C.EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION,)
    assert pending.blockers[0].details == {"status": "REQUIRES_REVIEW", "review_status": "PENDING"}


def test_only_current_annotations_document_a_location():
    superseded = ir.Annotation("EQUIPMENT_AREA", "ph_old", {"x": 0, "y": 0, "w": 0.2, "h": 0.2}, is_current=False, number=1)
    state = ready_state(annotations=(PANEL, superseded))
    result = ir.evaluate(state)
    assert result.codes[0] == C.LOCATIONS_NOT_DOCUMENTED
    assert result.blockers[0].details == {"missing": ["EQUIPMENT_AREA"], "legacy_geometry": []}


def test_legacy_rectangles_warn_at_submit_and_block_release():
    legacy = tuple(ir.Annotation(a.annotation_type, a.photo, a.geometry, geometry_space="LEGACY_CONTAINER", width_m=a.width_m, height_m=a.height_m) for a in (PANEL, EQUIPMENT))
    state = ready_state(annotations=legacy, approvals=(ir.Approval(1, "APPROVED", ir.build_location_snapshot(legacy)),))
    release = ir.evaluate(state)
    assert release.codes == (C.LOCATIONS_NOT_DOCUMENTED,)
    assert release.blockers[0].details == {"missing": [], "legacy_geometry": ["PANEL_AREA", "EQUIPMENT_AREA"]}
    submit = ir.field_completion(state)
    assert submit.ready and [w.code for w in submit.warnings] == [C.LEGACY_ANNOTATION_GEOMETRY]
    assert submit.as_dict()["warnings"][0]["details"] == {"annotation_types": ["PANEL_AREA", "EQUIPMENT_AREA"]}


def test_readiness_is_judged_on_the_row_after_the_change():
    """Legacy evaluated the INSTALLATION_READY gate on the pre-update row: an update that removed the panel photo in
    the same request as the release still passed."""
    before = ready_state()
    after = before.with_changes(panel_photo=None)
    assert ir.evaluate(before).ready
    assert ir.evaluate(after).codes == (C.PANEL_PHOTO_REQUIRED,)
    with pytest.raises(TypeError):
        before.with_changes(installation_readiness="INSTALLATION_READY")  # not a field anyone can set


def test_additional_work_rules():
    assert ir.evaluate(ready_state(additional_work=(ir.AdditionalWork("WALKWAY", required=False, status="REJECTED"),))).ready
    assert ir.evaluate(ready_state(additional_work=(ir.AdditionalWork("WALKWAY", customer_impacting=True, status="APPROVED"),))).ready
    assert ir.evaluate(ready_state(additional_work=(ir.AdditionalWork("WALKWAY", customer_impacting=True, status="NONE"),))).ready
    assert ir.evaluate(ready_state(additional_work=(ir.AdditionalWork("LADDER", status="COST_CALCULATED"),))).ready  # internal work does not wait for the customer
    both = ir.evaluate(
        ready_state(additional_work=(ir.AdditionalWork("CIVIL_WORK", customer_impacting=True, status="REJECTED"), ir.AdditionalWork("LADDER", customer_impacting=True, status="IDENTIFIED")))
    )
    assert both.codes == (C.ADDITIONAL_WORK_REJECTED, C.ADDITIONAL_WORK_UNAPPROVED)
    assert [b.details for b in both.blockers] == [{"work_types": ["CIVIL_WORK"]}, {"work_types": ["LADDER"]}]


@pytest.mark.parametrize(
    "complexity,reviews,pending",
    [
        ("ROUTINE", (), False),
        ("NOT_ASSESSED", (), False),
        ("ENGINEERING_REVIEW_REQUIRED", (), True),
        ("ENGINEERING_REVIEW_REQUIRED", (("ROUTINE", 1),), False),
        ("ENGINEERING_REVIEW_REQUIRED", (("RESOLVED", 1),), False),
        ("ROUTINE", (("PENDING", 1),), True),
        ("ROUTINE", (("REQUIRES_CHANGES", 1),), True),
        ("ROUTINE", (("REQUIRES_CHANGES", 1), ("RESOLVED", 2)), False),
        ("ROUTINE", (("RESOLVED", 2), ("REQUIRES_CHANGES", 1)), False),
        ("ENGINEERING_REVIEW_REQUIRED", (("REQUIRES_CHANGES", 1),), True),
    ],
)
def test_engineering_review_pending(complexity, reviews, pending):
    state = ready_state(complexity_status=complexity, engineering_reviews=tuple(ir.EngineeringReview(decision, sequence) for decision, sequence in reviews))
    assert ir.engineering_review_pending(state) is pending
    assert (C.ENGINEERING_REVIEW_PENDING in ir.evaluate(state).codes) is pending


# ---------------------------------------------------------------------------------------------------------------
# the location snapshot
# ---------------------------------------------------------------------------------------------------------------


def test_snapshot_shape():
    snapshot = ir.build_location_snapshot((PANEL, EQUIPMENT))
    assert snapshot["PANEL_AREA"] == {
        "photo": "ph_panel",
        "geometry": {"x": "0.100000", "y": "0.100000", "w": "0.500000", "h": "0.400000"},
        "geometry_space": "IMAGE",
        "width_m": "5.000",
        "height_m": "4.000",
        "area_m2": "20.000",
    }
    assert ir.build_location_snapshot((PANEL,))["EQUIPMENT_AREA"] is None


def test_snapshot_survives_a_json_round_trip_and_number_formats():
    import json

    stored = json.loads(json.dumps(ir.build_location_snapshot((PANEL, EQUIPMENT))))
    assert ir.location_snapshot_matches(stored, (PANEL, EQUIPMENT))
    same_as_floats = ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.4}, width_m=5.0, height_m=4, area_m2="20")
    assert ir.location_snapshot_matches(stored, (same_as_floats, EQUIPMENT))  # re-saving the same rectangle is not a change


@pytest.mark.parametrize(
    "changed",
    [
        ir.Annotation("PANEL_AREA", "ph_new", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}, width_m=5, height_m=4, area_m2=20),
        ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "w": 0.51, "h": 0.4}, width_m=5, height_m=4, area_m2=20),
        ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}, width_m=5.5, height_m=4, area_m2=22),
        ir.Annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}, geometry_space="LEGACY_CONTAINER", width_m=5, height_m=4, area_m2=20),
    ],
    ids=["new_photo", "moved", "remeasured", "geometry_space"],
)
def test_snapshot_detects_a_changed_location(changed):
    stored = ir.build_location_snapshot((PANEL, EQUIPMENT))
    assert not ir.location_snapshot_matches(stored, (changed, EQUIPMENT))


def test_out_of_range_numbers_never_crash_the_snapshot():
    """``geometry`` is JSONB: a value outside what the snapshot's fixed-point form can hold raised
    ``decimal.InvalidOperation`` (not a ValueError) from every readiness evaluation of that inspection."""
    import json

    huge = ir.Annotation("PANEL_AREA", "ph_panel", {"x": 1e30, "y": 0.1, "w": 0.5, "h": 0.4}, width_m=Decimal("1e40"), height_m=4, area_m2=20)
    snapshot = ir.build_location_snapshot((huge, EQUIPMENT))
    assert snapshot["PANEL_AREA"]["width_m"] == "1E+40" and snapshot["PANEL_AREA"]["geometry"]["x"] == "1E+30"
    assert ir.location_snapshot_matches(json.loads(json.dumps(snapshot)), (huge, EQUIPMENT))
    assert not ir.location_snapshot_matches(snapshot, (PANEL, EQUIPMENT))
    state = ready_state(annotations=(huge, EQUIPMENT), approvals=(ir.Approval(1, "APPROVED", snapshot),))
    assert ir.evaluate(state).ready


def test_a_missing_or_malformed_snapshot_never_matches():
    assert not ir.location_snapshot_matches(None, (PANEL, EQUIPMENT))
    assert not ir.location_snapshot_matches({}, (PANEL, EQUIPMENT))
    assert not ir.location_snapshot_matches({"PANEL_AREA": "x", "EQUIPMENT_AREA": None}, (PANEL, EQUIPMENT))
    result = ir.evaluate(ready_state(approvals=(ir.Approval(1, "APPROVED", None),)))
    assert result.codes == (C.CUSTOMER_APPROVAL_OUTDATED,)
    assert result.blockers[0].details == {"approval": 1, "snapshot_stored": False}


def test_the_latest_approved_approval_is_the_one_compared():
    old = ir.Approval(1, "APPROVED", ir.build_location_snapshot((PANEL, EQUIPMENT)))
    newer = ir.Approval(2, "APPROVED", ir.build_location_snapshot((MOVED, EQUIPMENT)))
    result = ir.evaluate(ready_state(annotations=(MOVED, EQUIPMENT), approvals=(newer, old)))
    assert result.ready and result.approved_location_number == 2
    superseded = ir.evaluate(ready_state(approvals=(ir.Approval(1, "SUPERSEDED", ir.build_location_snapshot((PANEL, EQUIPMENT))),)))
    assert superseded.codes == (C.CUSTOMER_APPROVAL_REQUIRED, C.LATEST_APPROVAL_NOT_APPROVED)


# ---------------------------------------------------------------------------------------------------------------
# field completion
# ---------------------------------------------------------------------------------------------------------------


def test_field_completion_accepts_any_recorded_verdict():
    for verdict in ("SUITABLE", "CONDITIONAL", "NOT_SUITABLE"):
        assert ir.field_completion(ready_state(site_suitability=verdict)).ready
    assert ir.field_completion(ready_state(site_suitability=None)).codes == (C.SUITABILITY_UNCONFIRMED,)


def test_field_completion_codes_and_order():
    blank = ir.InspectionState(system_type="HYBRID", complexity_status="ENGINEERING_REVIEW_REQUIRED")
    assert ir.field_completion(blank).codes == tuple(code for code in ir.FIELD_COMPLETION_CODES if code not in (C.COMPLEXITY_NOT_ASSESSED, C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE))
    assert C.COMPLEXITY_NOT_ASSESSED in ir.field_completion(ir.InspectionState()).codes


def test_field_completion_ignores_what_only_release_needs():
    state = ready_state(
        approvals=(),
        has_location_restrictions=True,
        wheeling_required=True,
        consumer_number=None,
        neutral_link="NOT_AVAILABLE",
        system_type="UNDECIDED",
        equipment=(),
        additional_work=(ir.AdditionalWork("WALKWAY", status="REJECTED"),),
    )
    assert ir.field_completion(state).ready
    failed = ready_state(equipment=(assessment("ON_GRID_INVERTER", height="FAIL"),))
    assert ir.field_completion(failed).ready  # answered in full; the review decides, not the submit


@pytest.mark.parametrize(
    "answer,review",
    [("FAIL", "WAIVED"), ("FAIL", "RESOLVED"), ("NEEDS_REVIEW", "RESOLVED"), ("FAIL", "PENDING")],
)
def test_a_critical_answer_never_completes_an_unanswered_checklist(answer, review):
    """One FAIL (or NEEDS_REVIEW) tap makes the status FAIL (REQUIRES_REVIEW), but the other 13 checks were never
    answered: submit and release must still say the assessment is incomplete — a waiver of the one failed check is
    not an assessment of the rest (§J 16, the one-PASS-tap defect in its FAIL form)."""
    partial = ir.EquipmentAssessment("ON_GRID_INVERTER", results={"direct_sunlight": answer}, review_status=review)
    assert partial.status in ("FAIL", "REQUIRES_REVIEW") and len(partial.unanswered) == 13
    state = ready_state(equipment=(partial,))
    release = ir.evaluate(state)
    assert release.codes == (C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE,)
    assert release.blockers[0].details == {"status": str(partial.status)}
    assert release.checks["equipment_resolved"] is False
    assert ir.field_completion(state).codes == (C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE,)
    hybrid = ready_state(system_type="HYBRID", equipment=(assessment("HYBRID_INVERTER"), ir.EquipmentAssessment("HYBRID_BATTERY", {"flooding": answer}, review_status=review)))
    assert ir.evaluate(hybrid).codes == (C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE,)
    assert ir.field_completion(hybrid).codes == (C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE,)


# ---------------------------------------------------------------------------------------------------------------
# state validation
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"site_suitability": "MAYBE"},
        {"complexity_status": "COMPLEX"},
        {"system_type": "on_grid"},
        {"neutral_link": "not available"},
        {"termination_point": "BROKEN"},
        {"equipment": (assessment("ON_GRID_INVERTER"), assessment("ON_GRID_INVERTER"))},
        {"approvals": (ir.Approval(1, "APPROVED"), ir.Approval(1, "PENDING"))},
        {"annotations": (PANEL, PANEL, EQUIPMENT)},
    ],
)
def test_state_is_validated(changes):
    with pytest.raises(ValueError):
        ready_state(**changes)


@pytest.mark.parametrize(
    "build",
    [
        lambda: ir.Annotation("ROOF", "p", {}),
        lambda: ir.Annotation("PANEL_AREA", "p", {}, geometry_space="SCREEN"),
        lambda: ir.Approval(1, "ACCEPTED"),
        lambda: ir.EquipmentAssessment("SOLAR_PANEL"),
        lambda: ir.EquipmentAssessment("ON_GRID_INVERTER", review_status="OPEN"),
        lambda: ir.AdditionalWork("WALKWAY", status="DONE"),
        lambda: ir.EngineeringReview("MAYBE"),
    ],
)
def test_parts_are_validated(build):
    with pytest.raises(ValueError):
        build()


def test_state_fields_lists_the_constructor_names():
    names = ir.state_fields()
    assert names[0] == "customer" and "engineering_reviews" in names and "installation_readiness" not in names
    assert ir.incomplete_code("HYBRID_BATTERY") == C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE
    assert ir.needs_resolution_code("ON_GRID_INVERTER") == C.EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION
