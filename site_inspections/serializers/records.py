"""Shapes of the inspection's child records: photos, annotations, equipment checklists, reviews, observations,
additional work and location approvals (staff side). Photos are always signed URLs; no field is commercial."""

from __future__ import annotations

from decimal import Decimal

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.serializers.customers import PhoneField
from site_inspections.models import AdditionalWorkItem, Annotation, EngineeringReview, EquipmentAssessment, LocationApproval, Observation, Photo
from site_inspections.models.choices import AnnotationType, AssessmentReview, ObservationCategory, PhotoType, WorkStatus, WorkType, WorkUnit
from site_inspections.serializers.common import SignedPhotoField, UserRefSerializer


class InspectionPhotoSerializer(serializers.ModelSerializer):
    file = SignedPhotoField()
    width = serializers.IntegerField(source="asset.width", read_only=True, allow_null=True)
    height = serializers.IntegerField(source="asset.height", read_only=True, allow_null=True)
    mime_type = serializers.CharField(source="asset.mime_type", read_only=True)

    class Meta:
        model = Photo
        fields = ["uid", "photo_type", "stage", "caption", "captured_at", "width", "height", "mime_type", "file", "version", "created_at"]
        read_only_fields = fields


class InspectionPhotoUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    photo_type = serializers.ChoiceField(choices=PhotoType.choices)
    stage = serializers.IntegerField(min_value=1, max_value=10, required=False, allow_null=True)
    caption = serializers.CharField(required=False, allow_blank=True, max_length=255)


class GeometrySerializer(serializers.Serializer):
    x = serializers.FloatField()
    y = serializers.FloatField()
    w = serializers.FloatField()
    h = serializers.FloatField()


class AnnotationSerializer(serializers.ModelSerializer):
    photo_uid = serializers.UUIDField(source="photo.uid", read_only=True)
    geometry = GeometrySerializer(read_only=True)

    class Meta:
        model = Annotation
        fields = ["uid", "annotation_type", "number", "is_current", "photo_uid", "geometry", "geometry_space", "width_m", "height_m", "area_m2", "created_at"]
        read_only_fields = fields


class AnnotationCreateSerializer(ExpectedVersionMixin):
    annotation_type = serializers.ChoiceField(choices=AnnotationType.choices)
    photo_uid = serializers.UUIDField()
    geometry = serializers.DictField(help_text="{x, y, w, h} in 0–1 image space; w and h ≥ 0.03; inside the image.")
    width_m = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    height_m = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)


class EquipmentAssessmentSerializer(serializers.ModelSerializer):
    evidence_photo_uid = serializers.SerializerMethodField()
    resolved_by = UserRefSerializer(read_only=True, allow_null=True)
    saved = serializers.SerializerMethodField()
    unanswered = serializers.SerializerMethodField()

    class Meta:
        model = EquipmentAssessment
        fields = [
            "uid",
            "equipment_type",
            "checks_version",
            "results",
            "status",
            "unanswered",
            "issue",
            "corrective_action",
            "engineer_remarks",
            "evidence_photo_uid",
            "review_status",
            "resolved_by",
            "resolved_at",
            "resolution_note",
            "saved",
            "version",
        ]
        read_only_fields = fields

    def get_evidence_photo_uid(self, obj) -> str | None:
        return str(obj.evidence_photo.uid) if obj.evidence_photo_id else None

    def get_saved(self, obj) -> bool:
        return obj.pk is not None

    def get_unanswered(self, obj) -> list[str]:
        from engines.inspection_checks import unanswered_checks

        return list(unanswered_checks(obj.equipment_type, obj.results or {}, obj.checks_version))


class EquipmentPutSerializer(ExpectedVersionMixin):
    results = serializers.DictField(child=serializers.CharField(), help_text="check id → PASS | FAIL | NEEDS_REVIEW | NOT_APPLICABLE | NOT_CHECKED (missing = NOT_CHECKED).")
    issue = serializers.CharField(required=False, allow_blank=True)
    corrective_action = serializers.CharField(required=False, allow_blank=True)
    engineer_remarks = serializers.CharField(required=False, allow_blank=True)
    evidence_photo_uid = serializers.UUIDField(required=False, allow_null=True)


class EquipmentReviewSerializer(ExpectedVersionMixin):
    decision = serializers.ChoiceField(choices=[AssessmentReview.RESOLVED, AssessmentReview.WAIVED])
    note = serializers.CharField()


class EngineeringReviewSerializer(serializers.ModelSerializer):
    requested_by = UserRefSerializer(read_only=True, allow_null=True)
    reviewer = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = EngineeringReview
        fields = ["uid", "trigger", "reason", "requested_by", "reviewer", "decision", "notes", "decided_at", "version", "created_at"]
        read_only_fields = fields


class ObservationSerializer(serializers.ModelSerializer):
    photo_uids = serializers.SerializerMethodField()

    class Meta:
        model = Observation
        fields = ["uid", "stage", "category", "note", "photo_uids", "created_at"]
        read_only_fields = fields

    def get_photo_uids(self, obj) -> list[str]:
        return [str(photo.uid) for photo in obj.photos.all()]


class ObservationCreateSerializer(serializers.Serializer):
    note = serializers.CharField()
    category = serializers.ChoiceField(choices=ObservationCategory.choices, required=False, default=ObservationCategory.GENERAL)
    stage = serializers.IntegerField(min_value=1, max_value=10, required=False, allow_null=True)
    photo_uids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=20)


class WorkItemSerializer(serializers.ModelSerializer):
    decided_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = AdditionalWorkItem
        fields = ["uid", "work_type", "required", "quantity", "unit", "dimensions", "reason", "customer_impacting", "status", "agreement_uid", "decided_by", "decided_at", "version", "created_at"]
        read_only_fields = fields


class WorkItemWriteSerializer(ExpectedVersionMixin):
    required = serializers.BooleanField(required=False)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    unit = serializers.ChoiceField(choices=WorkUnit.choices, required=False, allow_blank=True)
    dimensions = serializers.CharField(required=False, allow_blank=True, max_length=120)
    reason = serializers.CharField(required=False, allow_blank=True)
    customer_impacting = serializers.BooleanField(required=False)


class WorkItemCreateSerializer(WorkItemWriteSerializer):
    work_type = serializers.ChoiceField(choices=WorkType.choices)


class WorkTransitionSerializer(ExpectedVersionMixin):
    status = serializers.ChoiceField(choices=WorkStatus.choices)
    agreement_uid = serializers.UUIDField(required=False, allow_null=True, help_text="The EXTRA_STRUCTURE agreement (required for COST_CALCULATED).")
    note = serializers.CharField(required=False, allow_blank=True)


@extend_schema_serializer(component_name="SiteInspectionLocationApproval")
class ApprovalSerializer(serializers.ModelSerializer):
    approved_by_staff = UserRefSerializer(read_only=True, allow_null=True)
    otp_verified = serializers.SerializerMethodField()
    has_signature = serializers.SerializerMethodField()

    class Meta:
        model = LocationApproval
        fields = [
            "uid",
            "number",
            "status",
            "customer_name",
            "customer_phone_e164",
            "location_snapshot",
            "expires_at",
            "otp_verified",
            "responded_at",
            "responded_ip",
            "customer_comment",
            "has_signature",
            "approved_by_staff",
            "paper_reason",
            "created_at",
        ]
        read_only_fields = fields

    def get_otp_verified(self, obj) -> bool:
        return obj.otp_verified_at is not None

    def get_has_signature(self, obj) -> bool:
        return obj.signature_asset_id is not None


class ApprovalRequestSerializer(ExpectedVersionMixin):
    customer_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    customer_phone = PhoneField(required=False, allow_blank=True)


class ApprovalRequestedSerializer(serializers.Serializer):
    approval = ApprovalSerializer()
    link = serializers.CharField(help_text="Shown once: send it to the customer. Only its hash is stored.")


class PaperApprovalSerializer(ExpectedVersionMixin):
    scan = serializers.FileField(help_text="The paper-signed approval: PDF or photo.")
    reason = serializers.CharField()


class ActivitySerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    action = serializers.CharField()
    actor = UserRefSerializer(allow_null=True)
    actor_kind = serializers.CharField()
    before = serializers.JSONField(allow_null=True)
    after = serializers.JSONField(allow_null=True)
    note = serializers.CharField()
