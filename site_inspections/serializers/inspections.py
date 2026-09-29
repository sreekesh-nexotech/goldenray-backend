"""Inspection shapes: list, detail, create, stage allow-lists, workflow bodies.

The stage serializers are generated from ``services.stages.STAGES`` — exactly the columns of that stage are writable
(``PATCH …/stages/<stage>/``). No serializer here has a commercial field (``tests/test_engineer_boundary.py``).
"""

from __future__ import annotations

from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.serializers.customers import ActiveUserField, PhoneField
from site_inspections.models import Inspection, Snapshot
from site_inspections.models.choices import ReviewDecision, SystemType
from site_inspections.models.inspection import NON_NEGATIVE
from site_inspections.serializers.common import ComponentRefSerializer, CustomerRefSerializer, UserRefSerializer
from site_inspections.services import stages

IDENTITY_FIELDS = [
    "uid",
    "number",
    "visit_date",
    "origin",
    "system_type",
    "status",
    "held_from_status",
    "on_hold_reason",
    "complexity_status",
    "complexity_reason",
    "agreement_uid",
    "agreement_number",
    "agreement_version",
    "quotation_version_uid",
    "version",
    "created_at",
    "updated_at",
]
LAYOUT_FIELDS = ["panel_width_m", "panel_height_m", "panel_area_m2", "equipment_width_m", "equipment_height_m", "equipment_area_m2", "google_map_link"]
QUOTED_FIELDS = ["quoted_size_kw", "quoted_panel_capacity_w", "quoted_structure_type", "quoted_structure_material"]
STAGE_FIELD_LIST = [name for stage in stages.STAGES for name in stage.fields]
RANGES = {"latitude": (-90, 90), "longitude": (-180, 180), "shading_pct": (0, 100), "suitability_pct": (0, 100)}


def _uid(obj):
    return obj.uid if obj is not None else None


class InspectionListSerializer(serializers.ModelSerializer):
    customer = CustomerRefSerializer(read_only=True)
    engineer = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Inspection
        fields = [
            "uid",
            "number",
            "visit_date",
            "origin",
            "system_type",
            "status",
            "complexity_status",
            "customer",
            "engineer",
            "address",
            "pincode",
            "district",
            "location",
            "agreement_number",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class InspectionSerializer(serializers.ModelSerializer):
    """Detail: every technical column, grouped by stage in the model. No prices exist on this record."""

    customer = CustomerRefSerializer(read_only=True)
    engineer = UserRefSerializer(read_only=True, allow_null=True)
    released_by = UserRefSerializer(read_only=True, allow_null=True)
    quoted_panel = ComponentRefSerializer(read_only=True, allow_null=True)
    quoted_inverter = ComponentRefSerializer(read_only=True, allow_null=True)
    quoted_battery = ComponentRefSerializer(read_only=True, allow_null=True)
    panel_photo_uid = serializers.SerializerMethodField()
    equipment_photo_uid = serializers.SerializerMethodField()
    pre_sale_source_uid = serializers.SerializerMethodField()

    class Meta:
        model = Inspection
        fields = [
            *IDENTITY_FIELDS,
            "customer",
            "engineer",
            "pre_sale_source_uid",
            *STAGE_FIELD_LIST,
            *LAYOUT_FIELDS,
            "panel_photo_uid",
            "equipment_photo_uid",
            *QUOTED_FIELDS,
            "quoted_panel",
            "quoted_inverter",
            "quoted_battery",
            "released_at",
            "released_by",
        ]
        read_only_fields = fields

    def get_panel_photo_uid(self, obj) -> str | None:
        return str(obj.panel_photo.uid) if obj.panel_photo_id else None

    def get_equipment_photo_uid(self, obj) -> str | None:
        return str(obj.equipment_photo.uid) if obj.equipment_photo_id else None

    def get_pre_sale_source_uid(self, obj) -> str | None:
        return str(obj.pre_sale_source.uid) if obj.pre_sale_source_id else None


class InspectionCreateSerializer(serializers.Serializer):
    customer_uid = serializers.UUIDField()
    engineer_uid = ActiveUserField(required=False, allow_null=True, source="engineer")
    visit_date = serializers.DateField(required=False)
    system_type = serializers.ChoiceField(choices=SystemType.choices, required=False)
    address = serializers.CharField(required=False, allow_blank=True)
    pincode = serializers.RegexField(r"^[1-9][0-9]{5}$", required=False, allow_blank=True)
    location = serializers.CharField(required=False, allow_blank=True, max_length=120)
    district = serializers.CharField(required=False, allow_blank=True, max_length=100)
    quotation_version_uid = serializers.UUIDField(required=False, allow_null=True)


class _StageBase(ExpectedVersionMixin, serializers.ModelSerializer):
    """An allow-list: a key outside the stage's columns is a 400, never silently dropped."""

    def to_internal_value(self, data):
        allowed = set(self.fields)
        unknown = sorted(key for key in (data.keys() if hasattr(data, "keys") else ()) if key not in allowed)
        if unknown:
            raise serializers.ValidationError({key: ["Not writable in this stage."] for key in unknown})
        return super().to_internal_value(data)


def _stage_serializer(stage: stages.Stage) -> type[serializers.ModelSerializer]:
    extra = {name: {"required": False} for name in stage.fields}
    for name in stage.fields:
        if name in NON_NEGATIVE:
            extra[name]["min_value"] = 0
        if name in RANGES:
            extra[name]["min_value"], extra[name]["max_value"] = RANGES[name]
    attrs = {"Meta": type("Meta", (), {"model": Inspection, "fields": [*stage.fields, "expected_version"], "extra_kwargs": extra}), "__module__": __name__}
    if "registered_phone_e164" in stage.fields:
        attrs["registered_phone_e164"] = PhoneField(required=False, allow_blank=True, help_text="Any common spelling; stored as E.164.")
    name = "".join(part.title() for part in stage.key.split("-"))
    return type(f"Stage{name}Serializer", (_StageBase,), attrs)


STAGE_SERIALIZERS: dict[str, type[serializers.ModelSerializer]] = {stage.key: _stage_serializer(stage) for stage in stages.STAGES if stage.fields}


class InspectionAssignSerializer(ExpectedVersionMixin):
    engineer_uid = ActiveUserField(allow_null=True, source="engineer")


class SystemTypeSerializer(ExpectedVersionMixin):
    system_type = serializers.ChoiceField(choices=SystemType.choices)
    reason = serializers.CharField(required=False, allow_blank=True)


class InspectionReasonSerializer(ExpectedVersionMixin):
    reason = serializers.CharField()


class InspectionVersionSerializer(ExpectedVersionMixin):
    pass


class SnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = Snapshot
        fields = ["uid", "number", "source", "agreement_version", "data", "created_at"]
        read_only_fields = fields


class ReviewRequestSerializer(ExpectedVersionMixin):
    reason = serializers.CharField()


class ReviewDecisionSerializer(ExpectedVersionMixin):
    decision = serializers.ChoiceField(choices=[c for c in ReviewDecision.choices if c[0] != ReviewDecision.PENDING])
    notes = serializers.CharField(required=False, allow_blank=True)


class ReportJobSerializer(serializers.Serializer):
    job_uid = serializers.UUIDField(source="uid")
    status = serializers.CharField()
    variant = serializers.CharField(source="template")
    filename = serializers.SerializerMethodField()

    def get_filename(self, job) -> str:
        return (job.payload or {}).get("filename", "")
