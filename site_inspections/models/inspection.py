"""``site_inspections_inspection`` and ``site_inspections_snapshot`` (PLAN §2.7).

The Site Inspection V2 row (131 SQLite columns in 17 groups) as typed columns grouped by wizard stage — booleans are
booleans, measurements are decimals, every enum has a CHECK. There are **no commercial columns**: prices live on the
agreement and never reach this table. ``installation_readiness`` is not a column; it is derived by
``engines.inspection_readiness`` from the current row.

Agreement and quotation references are UUID columns without a foreign key (``agreement_uid``,
``quotation_version_uid``): the sales contexts above this one are built separately and are linked through outbox
events (``agreements.issued`` / ``agreements.superseded``) only.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from site_inspections.models.choices import (
    Availability,
    Complexity,
    Direction,
    GenerationImpact,
    Level,
    Origin,
    Phase,
    RoadAccess,
    RoofAccessibility,
    RoofCondition,
    RoofSlope,
    RoofStrength,
    RoofType,
    Shading,
    SnapshotSource,
    Status,
    StructureDecision,
    Suitability,
    SystemType,
    Tariff,
    VehicleType,
    values,
)

E164_RE = r"^\+[1-9][0-9]{7,14}$"
PINCODE_RE = r"^[1-9][0-9]{5}$"

# Enum columns that may be blank (not answered yet) → CHECK (blank or one of the values).
OPTIONAL_ENUMS = {
    "road_access": RoadAccess,
    "vehicle_type": VehicleType,
    "roof_type": RoofType,
    "roof_strength": RoofStrength,
    "roof_accessibility": RoofAccessibility,
    "roof_condition": RoofCondition,
    "installation_difficulty": Level,
    "roof_slope": RoofSlope,
    "direction_facing": Direction,
    "morning_shading": Shading,
    "afternoon_shading": Shading,
    "generation_impact": GenerationImpact,
    "phase": Phase,
    "tariff": Tariff,
    "neutral_link": Availability,
    "termination_point": Availability,
    "distribution_board": Availability,
    "earthing": Availability,
    "site_suitability": Suitability,
    "site_structure_type": StructureDecision,
}
REQUIRED_ENUMS = {"origin": Origin, "system_type": SystemType, "status": Status, "complexity_status": Complexity}
# Measurements: never negative.
NON_NEGATIVE = (
    "location_accuracy_m",
    "roof_length_m",
    "roof_width_m",
    "available_area_m2",
    "usable_area_m2",
    "connected_load_kw",
    "sanctioned_load_kw",
    "ac_cable_m",
    "dc_cable_m",
    "la_cable_m",
    "battery_cable_m",
    "quoted_size_kw",
    "walkway_length_m",
    "walkway_width_m",
    "ladder_length_m",
    "sliding_door_width_m",
    "sliding_door_height_m",
    "elevated_height_m",
    "panel_width_m",
    "panel_height_m",
    "panel_area_m2",
    "equipment_width_m",
    "equipment_height_m",
    "equipment_area_m2",
)


def _metres(**kwargs):
    return models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, **kwargs)


def _area(**kwargs):
    return models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, **kwargs)


def _choice(choices, length: int):
    return models.CharField(max_length=length, choices=choices.choices, blank=True, default="")


def _checks(table: str) -> list:
    constraints = [models.CheckConstraint(condition=Q(**{f"{name}__in": values(kind)}), name=f"{table}_{name}_valid") for name, kind in REQUIRED_ENUMS.items()]
    constraints += [models.CheckConstraint(condition=Q(**{name: ""}) | Q(**{f"{name}__in": values(kind)}), name=f"{table}_{name}_valid") for name, kind in OPTIONAL_ENUMS.items()]
    constraints += [models.CheckConstraint(condition=Q(**{f"{name}__isnull": True}) | Q(**{f"{name}__gte": 0}), name=f"{table}_{name}_non_negative") for name in NON_NEGATIVE]
    return constraints


class Inspection(BaseModel):
    """One site visit. Stage groups follow the ten-stage wizard; see ``services.stages`` for who may write what."""

    # ── identity ──────────────────────────────────────────────────────────────────────────────────────────────────
    number = models.CharField(max_length=24, help_text="SV-YYYYMMDD-NNNN (core_sequence_counter SV, office-local day).")
    visit_date = models.DateField()
    # The customer is a master record: PROTECT (customers are only soft-deleted; merges re-point this column).
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="+")
    # Assigned field engineer (record-scope anchor of the `assigned` scope): SET_NULL, deleting a user keeps the visit.
    engineer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    origin = models.CharField(max_length=14, choices=Origin.choices, default=Origin.PRE_SALE)
    system_type = models.CharField(max_length=10, choices=SystemType.choices, default=SystemType.UNDECIDED)
    status = models.CharField(max_length=28, choices=Status.choices, default=Status.DRAFT)
    held_from_status = models.CharField(max_length=28, choices=Status.choices, blank=True, default="", help_text="The status ON_HOLD returns to on resume.")
    complexity_status = models.CharField(max_length=28, choices=Complexity.choices, default=Complexity.NOT_ASSESSED)
    complexity_reason = models.TextField(blank=True, default="")
    agreement_uid = models.UUIDField(null=True, blank=True, help_text="The purchase agreement in force (agreements context; linked by event, no FK).")
    agreement_number = models.CharField(max_length=32, blank=True, default="")
    agreement_version = models.PositiveSmallIntegerField(null=True, blank=True)
    # The earlier pre-sale visit this agreement inspection follows (when that one could not be converted in place).
    pre_sale_source = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quotation_version_uid = models.UUIDField(null=True, blank=True)

    # ── location (stage 1) ────────────────────────────────────────────────────────────────────────────────────────
    address = models.TextField(blank=True, default="")
    pincode = models.CharField(max_length=6, blank=True, default="")
    location = models.CharField(max_length=120, blank=True, default="")
    district = models.CharField(max_length=100, blank=True, default="")
    google_map_link = models.CharField(max_length=500, blank=True, default="")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_accuracy_m = _metres()
    location_captured_at = models.DateTimeField(null=True, blank=True)
    # ── access (stage 1) ──────────────────────────────────────────────────────────────────────────────────────────
    road_access = _choice(RoadAccess, 16)
    vehicle_type = _choice(VehicleType, 16)
    access_remarks = models.TextField(blank=True, default="")
    # ── building / roof (stage 2) ─────────────────────────────────────────────────────────────────────────────────
    building_type = models.CharField(max_length=60, blank=True, default="")
    no_of_floors = models.PositiveSmallIntegerField(null=True, blank=True)
    roof_type = _choice(RoofType, 16)
    roof_strength = _choice(RoofStrength, 16)
    roof_accessibility = _choice(RoofAccessibility, 16)
    roof_accessibility_reason = models.TextField(blank=True, default="")
    roof_condition = _choice(RoofCondition, 16)
    installation_difficulty = _choice(Level, 8)
    roof_length_m = _metres()
    roof_width_m = _metres()
    available_area_m2 = _area()
    usable_area_m2 = _area()
    roof_slope = _choice(RoofSlope, 8)
    direction_facing = _choice(Direction, 4)
    # ── shading (stage 4) ─────────────────────────────────────────────────────────────────────────────────────────
    morning_shading = _choice(Shading, 8)
    afternoon_shading = _choice(Shading, 8)
    shade_source = models.CharField(max_length=255, blank=True, default="")
    shading_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    generation_impact = _choice(GenerationImpact, 16)
    shading_remarks = models.TextField(blank=True, default="")
    # ── electrical / KSEB (stage 5) ───────────────────────────────────────────────────────────────────────────────
    consumer_number = models.CharField(max_length=20, blank=True, default="")
    consumer_name = models.CharField(max_length=255, blank=True, default="")
    registered_phone_e164 = models.CharField(max_length=16, blank=True, default="")
    phase = _choice(Phase, 4)
    connected_load_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    sanctioned_load_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    meter_number = models.CharField(max_length=40, blank=True, default="")
    tariff = _choice(Tariff, 13)
    neutral_link = _choice(Availability, 20)
    termination_point = _choice(Availability, 20)
    distribution_board = _choice(Availability, 20)
    earthing = _choice(Availability, 20)
    electrical_remarks = models.TextField(blank=True, default="")
    wheeling_required = models.BooleanField(null=True, blank=True)
    # ── cables (stage 7) ──────────────────────────────────────────────────────────────────────────────────────────
    ac_cable_m = _metres()
    dc_cable_m = _metres()
    la_cable_m = _metres()
    battery_cable_m = _metres()
    battery_cable_route = models.TextField(blank=True, default="")
    cable_routing_remarks = models.TextField(blank=True, default="")
    # ── verdict (stage 10) ────────────────────────────────────────────────────────────────────────────────────────
    site_suitability = _choice(Suitability, 16)
    suitability_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    final_recommendation = models.TextField(blank=True, default="")
    engineer_remarks = models.TextField(blank=True, default="")
    additional_requirements = models.TextField(blank=True, default="")
    approval_remarks = models.TextField(blank=True, default="")
    # ── customer restrictions (stage 1) ───────────────────────────────────────────────────────────────────────────
    has_location_restrictions = models.BooleanField(default=False)
    customer_restrictions = models.TextField(blank=True, default="")
    customer_location_remarks = models.TextField(blank=True, default="")
    # ── quoted (technical copy of the agreement; written only by the linking service) ─────────────────────────────
    quoted_size_kw = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    # Catalog components are masters: SET_NULL keeps the visit if a component row ever goes (they are soft-deleted).
    quoted_panel = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quoted_panel_capacity_w = models.PositiveIntegerField(null=True, blank=True)
    # See quoted_panel.
    quoted_inverter = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # See quoted_panel.
    quoted_battery = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quoted_structure_type = models.CharField(max_length=32, blank=True, default="")
    quoted_structure_material = models.CharField(max_length=64, blank=True, default="")
    # ── structure / access work (stage 2) ─────────────────────────────────────────────────────────────────────────
    site_structure_type = _choice(StructureDecision, 28)
    walkway_required = models.BooleanField(default=False)
    walkway_length_m = _metres()
    walkway_width_m = _metres()
    ladder_required = models.BooleanField(default=False)
    ladder_length_m = _metres()
    sliding_door_required = models.BooleanField(default=False)
    sliding_door_width_m = _metres()
    sliding_door_height_m = _metres()
    elevated_height_m = _metres()
    # ── layout (stages 3 and 6; written from the current annotations) ─────────────────────────────────────────────
    # The reference photos are children of this inspection: SET_NULL (a photo is only soft-deleted, never while current).
    panel_photo = models.ForeignKey("site_inspections.Photo", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # See panel_photo.
    equipment_photo = models.ForeignKey("site_inspections.Photo", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    panel_width_m = _metres()
    panel_height_m = _metres()
    panel_area_m2 = _area()
    equipment_width_m = _metres()
    equipment_height_m = _metres()
    equipment_area_m2 = _area()
    # ── workflow ──────────────────────────────────────────────────────────────────────────────────────────────────
    released_at = models.DateTimeField(null=True, blank=True)
    # Attribution: SET_NULL.
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    on_hold_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "site_inspections_inspection"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="site_inspections_number_uniq"),
            models.UniqueConstraint(fields=["agreement_uid"], condition=Q(deleted_at__isnull=True, agreement_uid__isnull=False), name="site_inspections_one_per_agreement"),
            *_checks("site_inspections"),
            models.CheckConstraint(condition=Q(held_from_status="") | Q(held_from_status__in=values(Status)), name="site_inspections_held_from_valid"),
            models.CheckConstraint(condition=~Q(number=""), name="site_inspections_number_present"),
            models.CheckConstraint(condition=Q(registered_phone_e164="") | Q(registered_phone_e164__regex=E164_RE), name="site_inspections_phone_format"),
            models.CheckConstraint(condition=Q(pincode="") | Q(pincode__regex=PINCODE_RE), name="site_inspections_pincode_format"),
            models.CheckConstraint(condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90), name="site_inspections_latitude_range"),
            models.CheckConstraint(condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180), name="site_inspections_longitude_range"),
            models.CheckConstraint(condition=Q(shading_pct__isnull=True) | Q(shading_pct__gte=0, shading_pct__lte=100), name="site_inspections_shading_pct_range"),
            models.CheckConstraint(condition=Q(suitability_pct__isnull=True) | Q(suitability_pct__gte=0, suitability_pct__lte=100), name="site_inspections_suitability_pct_range"),
            models.CheckConstraint(condition=Q(origin="PRE_SALE") | Q(agreement_uid__isnull=False), name="site_inspections_agreement_origin"),
            models.CheckConstraint(condition=~Q(status="ON_HOLD") | (~Q(on_hold_reason="") & ~Q(held_from_status="")), name="site_inspections_hold_has_reason"),
            models.CheckConstraint(condition=~Q(status="INSTALLATION_READY") | Q(released_at__isnull=False), name="site_inspections_ready_released"),
        ]
        indexes = [
            models.Index(fields=["engineer", "status"], name="si_insp_engineer_status"),
            models.Index(fields=["status", "visit_date"], name="si_insp_status_visit"),
            models.Index(fields=["created_at"], name="si_insp_created"),
        ]

    def __str__(self) -> str:
        return self.number


class Snapshot(BaseModel):
    """What the inspection was asked to verify: the agreement (or pre-sale) technical snapshot, versioned.

    Written only by the services (creation, agreement link, agreement superseded); the current one is the highest
    ``number``. Same shape as the V2 ``buildInspectionSnapshot`` (no prices).
    """

    # A snapshot belongs to its inspection: CASCADE (true child).
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="snapshots")
    number = models.PositiveSmallIntegerField()
    source = models.CharField(max_length=10, choices=SnapshotSource.choices)
    agreement_version = models.PositiveSmallIntegerField(null=True, blank=True)
    data = models.JSONField(default=dict)

    class Meta:
        db_table = "site_inspections_snapshot"
        ordering = ["inspection_id", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["inspection", "number"], name="site_inspections_snapshot_number_uniq"),
            models.CheckConstraint(condition=Q(source__in=values(SnapshotSource)), name="site_inspections_snapshot_source_valid"),
            models.CheckConstraint(condition=Q(number__gte=1), name="site_inspections_snapshot_number_positive"),
        ]
