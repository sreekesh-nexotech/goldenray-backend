"""Site-inspection readiness scenarios, stated as legacy Site Inspection V2 rows.

Each scenario is the complete, ready BASE inspection with one change. ``scripts/parity/capture_si_legacy.py`` runs the
real legacy ``getReadiness`` / ``getFieldCompletion`` (``lib/site-inspection.ts``) over the legacy rows and stores the
blocker texts in ``golden/si_legacy.json``; :func:`to_state` maps the same rows onto the v4
:class:`~engines.inspection_readiness.InspectionState` the way the import does (suitability from the recommendation,
the two electrical fields apart, additional work as items with an explicit ``required`` flag, re-drawn rectangles,
approvals carrying the location snapshot of their request time).

``ready`` / ``completion`` are the v4 codes. Where the legacy engine answers differently, ``readiness_fix`` /
``completion_fix`` names the spec §J defect (or decision) responsible; everywhere else the legacy blockers, mapped to
codes through :data:`LEGACY_TEXT`, must equal the v4 codes in order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from engines import inspection_readiness as ir
from engines.inspection_checks import checklist

C = ir.Code
INSPECTION_ID = "si_parity"

BASE_ROW: dict = {
    "id": INSPECTION_ID,
    "customer_id": "cus_1",
    "engineer_id": "eng_1",
    "site_visit_date": "2026-09-01",
    "address": "Kaloor, Kochi",
    "panel_photo_id": "ph_panel",
    "equipment_photo_id": "ph_equipment",
    "panel_width": 5,
    "panel_height": 4,
    "equipment_width": 1.5,
    "equipment_height": 2,
    "has_location_restrictions": 0,
    "customer_location_remarks": None,
    "site_suitable_for_solar": 1,
    "final_recommendation": "SUITABLE",
    "complexity_status": "ROUTINE",
    "additional_work_status": "NONE",
    "walkway_required": "No",
    "ladder_required": "No",
    "sliding_door_required": "No",
    "underground_cabling": "NO",
    "extra_ac_cable": "NO",
    "extra_dc_cable": "NO",
    "new_neutral_link": "No",
    "new_termination": "No",
    "additional_earthing_required": None,
    "civil_work": None,
    "other_additional_work": None,
    "site_structure_type": "STANDARD",
    "quoted_structure_type": "STANDARD",
    "system_type": "ON_GRID",
    "inspection_snapshot_json": None,
    "wheeling_required": "No",
    "consumer_number": "1100000000001",
    "registered_phone": "9000000001",
    "neutral_link": "AVAILABLE",
    "termination_point": "AVAILABLE",
}


def annotation(kind: str, photo: str, geometry: dict, metadata: dict, created_at: str = "2026-09-01T10:00:00.000Z", row_id: str | None = None) -> dict:
    """A legacy ``site_inspection_annotations`` row (JSON columns as text, as SQLite holds them)."""
    return {
        "id": row_id or f"ann_{kind.lower()}_{created_at[:13].replace('-', '').replace('T', '_')}",
        "inspection_id": INSPECTION_ID,
        "photo_id": photo,
        "annotation_type": kind,
        "label": "Proposed panel area" if kind == "PANEL_AREA" else "Proposed equipment area",
        "geometry_json": json.dumps(geometry),
        "metadata_json": json.dumps(metadata),
        "created_at": created_at,
        "updated_at": created_at,
    }


PANEL = annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.4}, {"widthM": 5, "heightM": 4, "areaM2": 20})
EQUIPMENT = annotation("EQUIPMENT_AREA", "ph_equipment", {"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.3}, {"widthM": 1.5, "heightM": 2, "areaM2": 3})
PANEL_MOVED = annotation("PANEL_AREA", "ph_panel", {"x": 0.3, "y": 0.1, "width": 0.5, "height": 0.4}, {"widthM": 5, "heightM": 4, "areaM2": 20}, "2026-09-02T10:00:00.000Z")
PANEL_RESAVED = annotation("PANEL_AREA", "ph_panel", {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.4}, {"widthM": 5, "heightM": 4, "areaM2": 20}, "2026-09-02T10:00:00.000Z")


def approval(version: int, status: str, comment: str = "Location agreed") -> dict:
    return {
        "id": f"apr_{version}",
        "inspection_id": INSPECTION_ID,
        "approval_type": "INSTALLATION_LOCATION",
        "version": version,
        "status": status,
        "customer_comment": comment,
        "created_at": f"2026-09-0{version}T12:00:00.000Z",
    }


def all_results(kind: str, value: str = "PASS", **overrides: str) -> dict:
    results = {check_id: value for check_id in checklist(kind).ids}
    results.update(overrides)
    return results


def equipment(kind: str, results: dict, resolution_status: str = "OPEN", engineering_review_status: str = "NOT_REQUIRED") -> dict:
    """A legacy assessment row; the capture computes ``status`` with the legacy scorer, as the legacy PUT did."""
    return {"equipment_type": kind, "results_json": json.dumps(results), "resolution_status": resolution_status, "engineering_review_status": engineering_review_status}


BASE_ANNOTATIONS = (PANEL, EQUIPMENT)
BASE_APPROVALS = (approval(1, "APPROVED"),)
BASE_EQUIPMENT = (equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER")),)
HYBRID_EQUIPMENT = (equipment("HYBRID_INVERTER", all_results("HYBRID_INVERTER")), equipment("HYBRID_BATTERY", all_results("HYBRID_BATTERY")))


@dataclass(frozen=True)
class Scenario:
    id: str
    row: dict | None = None
    annotations: tuple | None = None
    approvals: tuple | None = None
    equipment: tuple | None = None
    approved_annotations: tuple | None = None  # rectangles at approval time (default: the current ones)
    legacy_snapshot: bool = False  # the legacy approval carries the __location_snapshot JSON its dead code expected
    v4_snapshot_stored: bool = True  # False: an approval imported from legacy without a snapshot
    legacy_geometry: bool = False  # the rectangles are still in legacy container space (not re-drawn)
    customer_impacting: bool = False  # v4 additional-work items are customer-impacting
    ready: tuple = ()
    completion: tuple = ()
    readiness_fix: str = ""
    completion_fix: str = ""

    def legacy_row(self) -> dict:
        return {**BASE_ROW, **(self.row or {})}

    def legacy_annotations(self) -> tuple:
        return BASE_ANNOTATIONS if self.annotations is None else self.annotations

    def legacy_approvals(self) -> tuple:
        rows = BASE_APPROVALS if self.approvals is None else self.approvals
        if not self.legacy_snapshot:
            return rows
        snapshot = legacy_location_snapshot(self.approved_annotations or self.legacy_annotations())
        return tuple({**row, "customer_comment": json.dumps({"customer_comment": row["customer_comment"], "__location_snapshot": snapshot})} for row in rows)

    def legacy_equipment(self) -> tuple:
        return BASE_EQUIPMENT if self.equipment is None else self.equipment

    def legacy_input(self) -> dict:
        return {"row": self.legacy_row(), "annotations": list(self.legacy_annotations()), "approvals": list(self.legacy_approvals()), "equipment": list(self.legacy_equipment())}


def legacy_location_snapshot(annotations: tuple) -> dict:
    """``buildLocationSnapshot`` as the legacy code shapes it (key order matters: it compares JSON strings)."""
    latest: dict = {}
    for row in sorted(annotations, key=lambda r: r["created_at"], reverse=True):
        latest.setdefault(row["annotation_type"], row)

    def entry(row):
        if row is None:
            return None
        return {
            "id": row["id"],
            "photo_id": row["photo_id"],
            "annotation_type": row["annotation_type"],
            "label": row["label"],
            "geometry": json.loads(row["geometry_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    return {"panel": entry(latest.get("PANEL_AREA")), "equipment": entry(latest.get("EQUIPMENT_AREA"))}


# ---------------------------------------------------------------------------------------------------------------
# legacy row → v4 state (what the Site Inspection import does)
# ---------------------------------------------------------------------------------------------------------------

#: Legacy flag column → v4 additional-work type.
WORK_FLAGS = {
    "walkway_required": "WALKWAY",
    "ladder_required": "LADDER",
    "sliding_door_required": "SLIDING_DOOR",
    "underground_cabling": "UNDERGROUND_CABLING",
    "extra_ac_cable": "EXTRA_AC_CABLE",
    "extra_dc_cable": "EXTRA_DC_CABLE",
    "new_neutral_link": ir.NEUTRAL_LINK_WORK,
    "new_termination": ir.TERMINATION_WORK,
    "additional_earthing_required": "ADDITIONAL_EARTHING",
    "civil_work": "CIVIL_WORK",
}
REVIEW_STATUS = {"RESOLVED": "RESOLVED", "WAIVED_APPROVED": "WAIVED"}


def legacy_bool(value) -> bool:
    """Legacy ``asBoolean``."""
    return value is True or value == 1 or str(value).lower() in ("1", "true", "yes")


def _annotations_v4(rows: tuple, legacy_geometry: bool) -> tuple[ir.Annotation, ...]:
    latest: dict = {}
    for row in sorted(rows, key=lambda r: r["created_at"], reverse=True):
        latest.setdefault(row["annotation_type"], row)
    out = []
    for row in latest.values():
        geometry = json.loads(row["geometry_json"])
        metadata = json.loads(row["metadata_json"])
        out.append(
            ir.Annotation(
                annotation_type=row["annotation_type"],
                photo=row["photo_id"],
                geometry={"x": geometry["x"], "y": geometry["y"], "w": geometry["width"], "h": geometry["height"]},
                geometry_space=ir.GeometrySpace.LEGACY_CONTAINER if legacy_geometry else ir.GeometrySpace.IMAGE,
                width_m=metadata.get("widthM"),
                height_m=metadata.get("heightM"),
                area_m2=metadata.get("areaM2"),
            )
        )
    return tuple(out)


def to_state(scenario: Scenario) -> ir.InspectionState:
    row = scenario.legacy_row()
    annotations = _annotations_v4(scenario.legacy_annotations(), scenario.legacy_geometry)
    approved_annotations = annotations if scenario.approved_annotations is None else _annotations_v4(scenario.approved_annotations, scenario.legacy_geometry)
    snapshot = ir.build_location_snapshot(approved_annotations) if scenario.v4_snapshot_stored else None
    approvals = tuple(
        ir.Approval(number=a["version"], status=a["status"], location_snapshot=snapshot if a["status"] == "APPROVED" else None)
        for a in (BASE_APPROVALS if scenario.approvals is None else scenario.approvals)
    )
    work_status = str(row.get("additional_work_status") or "NONE").upper()
    items = [
        ir.AdditionalWork(work_type=work_type, required=True, customer_impacting=scenario.customer_impacting, status=work_status if work_status != "NONE" else "IDENTIFIED")
        for column, work_type in WORK_FLAGS.items()
        if legacy_bool(row.get(column))
    ]
    if ir.has_value(row.get("other_additional_work")):
        items.append(ir.AdditionalWork(work_type="OTHER", required=True, customer_impacting=scenario.customer_impacting, status=work_status if work_status != "NONE" else "IDENTIFIED"))
    system_type = str(row.get("system_type") or "UNDECIDED").upper()
    return ir.InspectionState(
        customer=row.get("customer_id"),
        engineer=row.get("engineer_id"),
        visit_date=date.fromisoformat(row["site_visit_date"]) if row.get("site_visit_date") else None,
        address=row.get("address"),
        panel_photo=row.get("panel_photo_id"),
        equipment_photo=row.get("equipment_photo_id"),
        panel_width_m=row.get("panel_width"),
        panel_height_m=row.get("panel_height"),
        equipment_width_m=row.get("equipment_width"),
        equipment_height_m=row.get("equipment_height"),
        has_location_restrictions=legacy_bool(row.get("has_location_restrictions")),
        customer_location_remarks=row.get("customer_location_remarks"),
        site_suitability=ir.normalise_suitability(row.get("final_recommendation"), row.get("site_suitable_for_solar")),
        complexity_status=str(row.get("complexity_status") or "NOT_ASSESSED").upper(),
        system_type=system_type,
        wheeling_required=legacy_bool(row.get("wheeling_required")),
        consumer_number=row.get("consumer_number"),
        registered_phone_e164=row.get("registered_phone"),
        neutral_link=ir.normalise_availability(row.get("neutral_link")),
        termination_point=ir.normalise_availability(row.get("termination_point")),
        annotations=annotations,
        approvals=approvals,
        equipment=tuple(
            ir.EquipmentAssessment(equipment_type=e["equipment_type"], results=json.loads(e["results_json"]), review_status=REVIEW_STATUS.get(e["resolution_status"], "NOT_REQUIRED"))
            for e in scenario.legacy_equipment()
        ),
        additional_work=tuple(items),
    )


# ---------------------------------------------------------------------------------------------------------------
# legacy blocker text → code
# ---------------------------------------------------------------------------------------------------------------

LEGACY_TEXT: dict[str, str] = {
    **{text: str(code) for code, text in ir.BLOCKER_TEXT.items() if code not in (C.ADDITIONAL_WORK_UNAPPROVED, C.EQUIPMENT_SYSTEM_TYPE_UNDECIDED, C.LEGACY_ANNOTATION_GEOMETRY)},
    # legacy-only wording, mapped to tokens that name what the legacy engine did
    **{f"{kind.replace('_', ' ')} assessment must pass before installation": f"LEGACY_{kind}_MUST_PASS" for kind in ("ON_GRID_INVERTER", "HYBRID_INVERTER", "HYBRID_BATTERY")},
    "Proposed equipment-area reference photo is required": str(C.EQUIPMENT_PHOTO_REQUIRED),
    "Panel area dimensions are required": "PANEL_DIMENSIONS",
    "Equipment area dimensions are required": "EQUIPMENT_DIMENSIONS",
    "Conditional site suitability requires resolution before completion": "LEGACY_CONDITIONAL_AT_COMPLETION",
}

#: v4 splits the legacy completion "dimensions" blocker by side; compared collapsed.
DIMENSION_COLLAPSE = {
    str(C.PANEL_WIDTH_REQUIRED): "PANEL_DIMENSIONS",
    str(C.PANEL_HEIGHT_REQUIRED): "PANEL_DIMENSIONS",
    str(C.EQUIPMENT_WIDTH_REQUIRED): "EQUIPMENT_DIMENSIONS",
    str(C.EQUIPMENT_HEIGHT_REQUIRED): "EQUIPMENT_DIMENSIONS",
}

OG_INCOMPLETE = C.EQUIPMENT_ON_GRID_INVERTER_INCOMPLETE
OG_RESOLUTION = C.EQUIPMENT_ON_GRID_INVERTER_NEEDS_RESOLUTION
PASS_TAP = (equipment("ON_GRID_INVERTER", {"direct_sunlight": "PASS"}),)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("base_ready"),
    Scenario("no_customer", row={"customer_id": None}, ready=(C.CUSTOMER_REQUIRED,), completion=(C.CUSTOMER_REQUIRED,)),
    Scenario("no_engineer", row={"engineer_id": None}, ready=(C.ENGINEER_REQUIRED,), completion=(C.ENGINEER_REQUIRED,)),
    Scenario("no_visit_date", row={"site_visit_date": None}, ready=(C.VISIT_DATE_REQUIRED,), completion=(C.VISIT_DATE_REQUIRED,)),
    Scenario("no_address", row={"address": "  "}, completion=(C.SITE_ADDRESS_REQUIRED,)),
    Scenario("no_panel_photo", row={"panel_photo_id": None}, ready=(C.PANEL_PHOTO_REQUIRED,), completion=(C.PANEL_PHOTO_REQUIRED,)),
    Scenario("no_equipment_photo", row={"equipment_photo_id": None}, ready=(C.EQUIPMENT_PHOTO_REQUIRED,), completion=(C.EQUIPMENT_PHOTO_REQUIRED,)),
    Scenario("panel_width_zero", row={"panel_width": 0}, ready=(C.PANEL_WIDTH_REQUIRED,), completion=(C.PANEL_WIDTH_REQUIRED,)),
    Scenario("panel_height_missing", row={"panel_height": None}, ready=(C.PANEL_HEIGHT_REQUIRED,), completion=(C.PANEL_HEIGHT_REQUIRED,)),
    Scenario(
        "equipment_width_negative",
        row={"equipment_width": -1.5},
        ready=(C.EQUIPMENT_WIDTH_REQUIRED,),
        completion=(C.EQUIPMENT_WIDTH_REQUIRED,),
        completion_fix="§C.3: legacy completion only tested that a number was present — a negative width passed",
    ),
    Scenario("equipment_height_text", row={"equipment_height": "two"}, ready=(C.EQUIPMENT_HEIGHT_REQUIRED,), completion=(C.EQUIPMENT_HEIGHT_REQUIRED,)),
    Scenario("one_location_documented", annotations=(PANEL,), approved_annotations=(PANEL,), ready=(C.LOCATIONS_NOT_DOCUMENTED,), completion=(C.LOCATIONS_NOT_DOCUMENTED,)),
    Scenario(
        "legacy_container_rectangles",
        legacy_geometry=True,
        ready=(C.LOCATIONS_NOT_DOCUMENTED,),
        readiness_fix="Plan 2 D2-13: legacy container-relative rectangles must be re-drawn before release",
    ),
    Scenario("restrictions_without_remarks", row={"has_location_restrictions": 1}, ready=(C.RESTRICTIONS_NEED_REMARKS,)),
    Scenario("restrictions_blank_remarks", row={"has_location_restrictions": "yes", "customer_location_remarks": "   "}, ready=(C.RESTRICTIONS_NEED_REMARKS,)),
    Scenario("restrictions_with_remarks", row={"has_location_restrictions": 1, "customer_location_remarks": "Keep the terrace door clear"}),
    Scenario(
        "suitability_missing",
        row={"site_suitable_for_solar": None, "final_recommendation": None},
        ready=(C.SUITABILITY_UNCONFIRMED,),
        completion=(C.SUITABILITY_UNCONFIRMED,),
    ),
    Scenario(
        "not_suitable",
        row={"site_suitable_for_solar": 0, "final_recommendation": "NOT_SUITABLE"},
        ready=(C.SITE_NOT_SUITABLE,),
        completion_fix="§J 20: completion demanded SUITABLE, so a not-suitable site could never be completed",
    ),
    Scenario(
        "conditional_recommendation_stored_as_zero",
        row={"site_suitable_for_solar": 0, "final_recommendation": "CONDITIONAL"},
        ready=(C.SUITABILITY_CONDITIONAL,),
        readiness_fix="§J 19: CONDITIONAL wrote 0 and read back as NOT_SUITABLE",
        completion_fix="§J 19/20: read as NOT_SUITABLE, and completion demanded SUITABLE",
    ),
    Scenario(
        "conditional_flag",
        row={"site_suitable_for_solar": "CONDITIONAL", "final_recommendation": None},
        ready=(C.SUITABILITY_CONDITIONAL,),
        completion_fix="§J 20: completion demanded SUITABLE",
    ),
    Scenario(
        "unknown_suitability",
        row={"site_suitable_for_solar": "MAYBE", "final_recommendation": None},
        ready=(C.SUITABILITY_UNCONFIRMED,),
        completion=(C.SUITABILITY_UNCONFIRMED,),
        readiness_fix="§J 19: an unknown suitability string passed readiness",
        completion_fix="§J 19: an unknown suitability string passed completion",
    ),
    Scenario("no_approval", approvals=(), ready=(C.CUSTOMER_APPROVAL_REQUIRED,)),
    Scenario(
        "location_moved_after_approval",
        annotations=(PANEL, EQUIPMENT, PANEL_MOVED),
        approved_annotations=(PANEL, EQUIPMENT),
        ready=(C.CUSTOMER_APPROVAL_OUTDATED,),
        readiness_fix="§J 7: the approval never stored a location snapshot, so a moved rectangle kept its approval",
    ),
    Scenario(
        "location_moved_legacy_snapshot",
        annotations=(PANEL, EQUIPMENT, PANEL_MOVED),
        approved_annotations=(PANEL, EQUIPMENT),
        legacy_snapshot=True,
        ready=(C.CUSTOMER_APPROVAL_OUTDATED,),
    ),
    Scenario(
        "rectangle_resaved_unchanged",
        annotations=(PANEL, EQUIPMENT, PANEL_RESAVED),
        approved_annotations=(PANEL, EQUIPMENT),
        legacy_snapshot=True,
        readiness_fix="§C.1-13: the legacy comparison included row ids and timestamps, so re-saving the same rectangle voided the approval",
    ),
    Scenario(
        "approval_imported_without_snapshot",
        v4_snapshot_stored=False,
        ready=(C.CUSTOMER_APPROVAL_OUTDATED,),
        readiness_fix="§J 7 (fail closed): an approval without a stored snapshot cannot prove the location is current",
    ),
    Scenario(
        "complex_site",
        row={"complexity_status": "ENGINEERING_REVIEW_REQUIRED"},
        ready=(C.ENGINEERING_REVIEW_PENDING,),
        completion=(C.ENGINEERING_REVIEW_PENDING,),
        completion_fix="Plan 2 §3.2: submit requires no pending engineering review",
    ),
    Scenario("complexity_not_assessed", row={"complexity_status": "NOT_ASSESSED"}, completion=(C.COMPLEXITY_NOT_ASSESSED,)),
    Scenario(
        "walkway_rejected",
        row={"walkway_required": "Yes", "additional_work_status": "REJECTED"},
        ready=(C.ADDITIONAL_WORK_REJECTED,),
    ),
    Scenario(
        "structure_type_differs_rejected",
        row={"site_structure_type": "ELEVATED", "additional_work_status": "REJECTED"},
        readiness_fix="§J 22: any non-STANDARD structure type made every inspection 'require' additional work",
    ),
    Scenario(
        "customer_impacting_work_unapproved",
        row={"walkway_required": "Yes", "additional_work_status": "IDENTIFIED"},
        customer_impacting=True,
        ready=(C.ADDITIONAL_WORK_UNAPPROVED,),
        readiness_fix="§J 22 / PLAN D-12: customer-impacting additional work never gated release",
    ),
    Scenario("customer_impacting_work_approved", row={"walkway_required": "Yes", "additional_work_status": "APPROVED"}, customer_impacting=True),
    Scenario("internal_work_identified", row={"ladder_required": "Yes", "additional_work_status": "IDENTIFIED"}),
    Scenario("equipment_missing", equipment=(), ready=(OG_INCOMPLETE,), completion=(OG_INCOMPLETE,), completion_fix="§J 20: completion ignored the equipment assessments"),
    Scenario(
        "one_pass_tap",
        equipment=PASS_TAP,
        ready=(OG_INCOMPLETE,),
        completion=(OG_INCOMPLETE,),
        readiness_fix="§J 16: status over the submitted keys only — one PASS tap made the assessment PASS",
        completion_fix="§J 16/20",
    ),
    Scenario(
        "explicit_not_checked",
        equipment=(equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER", height="NOT_CHECKED")),),
        ready=(OG_INCOMPLETE,),
        completion=(OG_INCOMPLETE,),
        completion_fix="§J 20: completion ignored the equipment assessments",
    ),
    Scenario("all_not_applicable", equipment=(equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER", "NOT_APPLICABLE")),)),
    Scenario(
        "failed_check",
        equipment=(equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER", ventilation="FAIL")),),
        ready=(OG_RESOLUTION,),
        readiness_fix="§C.1-16: a FAIL produced two blockers (resolution + must pass)",
    ),
    Scenario(
        "failed_check_waived",
        equipment=(equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER", ventilation="FAIL"), resolution_status="WAIVED_APPROVED"),),
        readiness_fix="§J 16: resolution/waiver was ignored — there was no path to release",
    ),
    Scenario(
        "review_resolved",
        equipment=(equipment("ON_GRID_INVERTER", all_results("ON_GRID_INVERTER", heat_source="NEEDS_REVIEW"), resolution_status="RESOLVED"),),
        readiness_fix="§J 16: resolution/waiver was ignored — there was no path to release",
    ),
    Scenario(
        "partial_fail_waived",
        equipment=(equipment("ON_GRID_INVERTER", {"direct_sunlight": "FAIL"}, resolution_status="WAIVED_APPROVED"),),
        ready=(OG_INCOMPLETE,),
        completion=(OG_INCOMPLETE,),
        readiness_fix="§J 16: status over the submitted keys only — one FAIL tap scored FAIL; waiving it must not stand in for the 13 checks never answered",
        completion_fix="§J 16/20: completion ignored the equipment assessments",
    ),
    Scenario("hybrid_complete", row={"system_type": "HYBRID"}, equipment=HYBRID_EQUIPMENT),
    Scenario(
        "hybrid_battery_missing",
        row={"system_type": "HYBRID"},
        equipment=HYBRID_EQUIPMENT[:1],
        ready=(C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE,),
        completion=(C.EQUIPMENT_HYBRID_BATTERY_INCOMPLETE,),
        completion_fix="§J 20: completion ignored the equipment assessments",
    ),
    Scenario(
        "undecided_system_type",
        row={"system_type": "UNDECIDED"},
        ready=(C.EQUIPMENT_SYSTEM_TYPE_UNDECIDED,),
        readiness_fix="§J 13: UNDECIDED silently waived every equipment assessment",
    ),
    Scenario(
        "system_type_only_in_snapshot",
        row={"system_type": None, "inspection_snapshot_json": json.dumps({"system": {"type": "HYBRID"}})},
        ready=(C.EQUIPMENT_SYSTEM_TYPE_UNDECIDED,),
        readiness_fix="§J 17: readiness fell back to the snapshot while other readers used the column; v4 reads the column only",
    ),
    Scenario("wheeling_without_consumer_number", row={"wheeling_required": "Yes", "consumer_number": None}, ready=(C.WHEELING_CONSUMER_NUMBER_REQUIRED,)),
    Scenario("wheeling_without_phone", row={"wheeling_required": "yes", "registered_phone": ""}, ready=(C.WHEELING_PHONE_REQUIRED,)),
    Scenario(
        "wheeling_consumer_number_zero",
        row={"wheeling_required": "Yes", "consumer_number": 0},
        readiness_fix="§J 30: hasValue(0) treated the value 0 as missing",
    ),
    Scenario(
        "neutral_not_available_ui_spelling",
        row={"neutral_link": "NOT_AVAILABLE", "termination_point": "NOT_AVAILABLE"},
        ready=(C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,),
        readiness_fix="§J 18: compared 'not available' against the stored NOT_AVAILABLE, so it never fired",
    ),
    Scenario(
        "neutral_not_available_legacy_spelling",
        row={"neutral_link": "not available", "termination_point": "not available"},
        ready=(C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,),
    ),
    Scenario("neutral_with_new_neutral_link", row={"neutral_link": "not available", "termination_point": "not available", "new_neutral_link": "Yes", "new_termination": "Yes"}),
    Scenario(
        "termination_needs_modification_only",
        row={"termination_point": "NEEDS_MODIFICATION"},
        ready=(C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,),
        readiness_fix="§J 18: the two fields were collapsed into one and the termination value was lost",
    ),
    Scenario(
        "neutral_fixed_by_new_termination",
        row={"neutral_link": "needs modification", "termination_point": "AVAILABLE", "new_termination": "Yes"},
        ready=(C.NEUTRAL_OR_TERMINATION_DECISION_REQUIRED,),
        readiness_fix="§J 18: a new termination point was accepted as the decision for a defective neutral link",
    ),
    Scenario(
        "latest_approval_pending",
        approvals=(approval(1, "APPROVED"), approval(2, "PENDING")),
        ready=(C.LATEST_APPROVAL_NOT_APPROVED,),
    ),
    Scenario(
        "many_blockers_in_order",
        row={"customer_id": None, "site_suitable_for_solar": None, "final_recommendation": None, "wheeling_required": "Yes", "consumer_number": None},
        equipment=(),
        approvals=(approval(1, "REJECTED"),),
        ready=(C.CUSTOMER_REQUIRED, C.SUITABILITY_UNCONFIRMED, C.CUSTOMER_APPROVAL_REQUIRED, OG_INCOMPLETE, C.WHEELING_CONSUMER_NUMBER_REQUIRED, C.LATEST_APPROVAL_NOT_APPROVED),
        completion=(C.CUSTOMER_REQUIRED, C.SUITABILITY_UNCONFIRMED, OG_INCOMPLETE),
        completion_fix="§J 20: completion ignored the equipment assessments",
    ),
)

#: Result maps whose legacy status (``calculateAssessmentStatus``) the capture records.
RESULT_SETS: dict[str, dict] = {
    "empty": {},
    "all_not_checked": {"direct_sunlight": "NOT_CHECKED", "rain_exposure": "NOT_CHECKED"},
    "one_pass": {"direct_sunlight": "PASS"},
    "one_na": {"direct_sunlight": "NOT_APPLICABLE"},
    "pass_and_not_checked": {"direct_sunlight": "PASS", "rain_exposure": "NOT_CHECKED"},
    "fail_and_review": {"direct_sunlight": "FAIL", "rain_exposure": "NEEDS_REVIEW"},
    "review_and_not_checked": {"direct_sunlight": "NEEDS_REVIEW", "rain_exposure": "NOT_CHECKED"},
    "all_pass": all_results("ON_GRID_INVERTER"),
    "all_na": all_results("ON_GRID_INVERTER", "NOT_APPLICABLE"),
    "all_pass_one_na": all_results("ON_GRID_INVERTER", height="NOT_APPLICABLE"),
    "all_pass_one_fail": all_results("ON_GRID_INVERTER", height="FAIL"),
    "all_pass_one_review": all_results("ON_GRID_INVERTER", height="NEEDS_REVIEW"),
    "all_pass_one_not_checked": all_results("ON_GRID_INVERTER", height="NOT_CHECKED"),
    "partial_fail": {"direct_sunlight": "FAIL"},
    "unknown_value": {"direct_sunlight": "MAYBE"},
}
