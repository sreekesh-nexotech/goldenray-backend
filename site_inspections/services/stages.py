"""The ten wizard stages (Site Inspection V2 ``stages.ts``) and the columns each one may write — the allow-lists.

``PATCH site-inspections/<uid>/stages/<stage>/`` accepts exactly the fields of its stage (the serializer is built
from the same list and the service checks it again). Never writable through a stage: ``status``, ``system_type``,
``origin``, ``agreement_*``, ``engineer``, ``customer``, ``number``, ``visit_date``, the ``quoted_*`` columns and the
layout columns (written by the annotations service from the current rectangles), nor anything commercial.

Stages without columns (panel area, equipment, evidence) are worked through their own endpoints: annotations, the
equipment checklists and photos.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    number: int
    key: str
    title: str
    fields: tuple[str, ...]


STAGES: tuple[Stage, ...] = (
    Stage(
        1,
        "site-access",
        "Site & access",
        (
            "address",
            "pincode",
            "location",
            "district",
            "latitude",
            "longitude",
            "location_accuracy_m",
            "location_captured_at",
            "road_access",
            "vehicle_type",
            "access_remarks",
            "has_location_restrictions",
            "customer_restrictions",
            "customer_location_remarks",
        ),
    ),
    Stage(
        2,
        "roof-structure",
        "Roof & structure",
        (
            "building_type",
            "no_of_floors",
            "roof_type",
            "roof_condition",
            "roof_strength",
            "roof_accessibility",
            "roof_accessibility_reason",
            "installation_difficulty",
            "roof_length_m",
            "roof_width_m",
            "available_area_m2",
            "usable_area_m2",
            "roof_slope",
            "direction_facing",
            "site_structure_type",
            "elevated_height_m",
            "additional_requirements",
            "walkway_required",
            "walkway_length_m",
            "walkway_width_m",
            "ladder_required",
            "ladder_length_m",
            "sliding_door_required",
            "sliding_door_width_m",
            "sliding_door_height_m",
        ),
    ),
    Stage(3, "panel-area", "Solar panel area", ()),
    Stage(4, "shading", "Shading", ("morning_shading", "afternoon_shading", "shade_source", "shading_pct", "generation_impact", "shading_remarks")),
    Stage(
        5,
        "electrical",
        "Electrical / KSEB",
        (
            "consumer_number",
            "consumer_name",
            "registered_phone_e164",
            "wheeling_required",
            "meter_number",
            "tariff",
            "phase",
            "sanctioned_load_kw",
            "connected_load_kw",
            "neutral_link",
            "termination_point",
            "distribution_board",
            "earthing",
            "electrical_remarks",
        ),
    ),
    Stage(6, "equipment", "Equipment location", ()),
    Stage(7, "cable-routing", "Cable routing", ("ac_cable_m", "dc_cable_m", "la_cable_m", "battery_cable_m", "battery_cable_route", "cable_routing_remarks")),
    Stage(8, "additional-work", "Additional work", ("complexity_status", "complexity_reason")),
    Stage(9, "evidence", "Evidence", ()),
    Stage(10, "engineering-review", "Engineering review", ("site_suitability", "suitability_pct", "final_recommendation", "engineer_remarks", "approval_remarks")),
)

BY_KEY: dict[str, Stage] = {stage.key: stage for stage in STAGES}
EDITABLE_KEYS: tuple[str, ...] = tuple(stage.key for stage in STAGES if stage.fields)
ALL_STAGE_FIELDS: frozenset[str] = frozenset(field for stage in STAGES for field in stage.fields)


def stage(key: str) -> Stage | None:
    return BY_KEY.get(key)
