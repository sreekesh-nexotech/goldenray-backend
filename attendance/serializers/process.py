"""Recompute requests and results (``process/``, ``process-all/``, ``recalculate/``) and corrections."""

from __future__ import annotations

from rest_framework import serializers

from attendance.models import AttendanceCorrection
from attendance.serializers.common import CORRECTION_FIELD_CHOICES, TRANSPORT_CHOICES, AttendanceDeviceRefSerializer, EmployeeRefSerializer, InputDateField
from core.serializers import ExpectedVersionMixin
from hr.serializers.refs import HrActorRefSerializer


class ProcessRequestSerializer(serializers.Serializer):
    date_from = InputDateField()
    date_to = InputDateField()
    employee_uids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True, max_length=1000, help_text="Default: every employee.")

    def validate(self, attrs):
        if attrs["date_to"] < attrs["date_from"]:
            raise serializers.ValidationError({"date_to": "Must not be before date_from."})
        return attrs


class RecalculateRequestSerializer(serializers.Serializer):
    date_from = InputDateField(required=False, help_text="Default: yesterday (today is never stored, A7).")
    date_to = InputDateField(required=False)
    employee_uids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=True, max_length=1000)


class RecomputeResultSerializer(serializers.Serializer):
    date_from = serializers.CharField(help_text="As requested.")
    date_to = serializers.CharField()
    computed_from = serializers.CharField()
    computed_to = serializers.CharField(help_text="Never later than the latest possible today; days ≥ today per office are skipped (A7).")
    not_recomputed_before = serializers.CharField(allow_null=True)
    reason = serializers.CharField()
    employees = serializers.IntegerField()
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    removed = serializers.IntegerField(help_text="Stored days outside the person's employment dates, soft-deleted.")
    skipped_corrected = serializers.IntegerField()
    skipped_future = serializers.IntegerField()
    absent_days = serializers.IntegerField()
    raw_punches_considered = serializers.IntegerField()
    unmapped_pins = serializers.IntegerField()


class ProcessAllResultSerializer(serializers.Serializer):
    queued = serializers.BooleanField()
    date_from = serializers.DateField(allow_null=True)
    date_to = serializers.DateField(allow_null=True)
    requests = serializers.IntegerField()


class RecalculateDeviceSerializer(serializers.Serializer):
    device = AttendanceDeviceRefSerializer()
    transport = serializers.ChoiceField(choices=TRANSPORT_CHOICES)
    connection_state = serializers.CharField(allow_null=True)
    last_sync_at = serializers.DateTimeField(allow_null=True)
    last_punch_at = serializers.DateTimeField(allow_null=True)
    note = serializers.CharField()


class RecalculateSummarySerializer(serializers.Serializer):
    devices = serializers.IntegerField()
    without_transport = serializers.IntegerField()


class RecalculateResultSerializer(serializers.Serializer):
    result = RecomputeResultSerializer()
    devices = RecalculateDeviceSerializer(many=True)
    summary = RecalculateSummarySerializer()


class AttendanceCorrectionSerializer(serializers.ModelSerializer):
    day_uid = serializers.UUIDField(source="day.uid", read_only=True)
    employee = EmployeeRefSerializer(source="day.employee", read_only=True)
    work_date = serializers.DateField(source="day.work_date", read_only=True)
    created_by = HrActorRefSerializer(read_only=True, allow_null=True)
    revoked_by = HrActorRefSerializer(read_only=True, allow_null=True)
    is_active = serializers.BooleanField(read_only=True)
    old = serializers.JSONField(read_only=True, allow_null=True)
    new = serializers.JSONField(read_only=True, allow_null=True)

    class Meta:
        model = AttendanceCorrection
        fields = ["uid", "day_uid", "employee", "work_date", "field", "old", "new", "reason", "is_active", "created_at", "created_by", "revoked_at", "revoked_by", "revoke_reason", "version"]
        read_only_fields = fields


class CorrectionCreateSerializer(ExpectedVersionMixin, serializers.Serializer):
    day_uid = serializers.UUIDField()
    field = serializers.ChoiceField(choices=CORRECTION_FIELD_CHOICES)
    new = serializers.JSONField(
        allow_null=True, help_text="The value: a status (PRESENT…), whole minutes, true/false, or an office wall-clock time `YYYY-MM-DDTHH:MM[:SS]` (or null) for first_in/last_out."
    )
    reason = serializers.CharField(max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1, help_text="The day's version.")


class CorrectionRevokeSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1, help_text="The correction's version.")


class CorrectionQuerySerializer(serializers.Serializer):
    employee = serializers.UUIDField(required=False)
    day = serializers.UUIDField(required=False)
    active = serializers.BooleanField(required=False, allow_null=True, default=None)
