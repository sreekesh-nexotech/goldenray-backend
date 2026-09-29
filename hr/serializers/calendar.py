"""Holiday, leave type, leave record and attendance rule shapes."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, LeaveType, Office, Shift
from hr.serializers.refs import EmployeeRefSerializer, HrActorRefSerializer, LeaveTypeRefSerializer, OfficeRefSerializer, ShiftRefSerializer
from hr.services.holidays import holidays_queryset
from hr.services.leave import leave_queryset, leave_types_queryset
from hr.services.rules import MINUTE_LIMITS, RECOGNISED_KEYS, rules_queryset


def _all_optional(fields):
    for name, field in fields.items():
        if name != "expected_version":
            field.required = False
            field.default = serializers.empty
    return fields


# ----------------------------------------------------------------------------------------------------------------
# Holidays
# ----------------------------------------------------------------------------------------------------------------
class HolidaySerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True, help_text="Null: every office.")
    is_global = serializers.SerializerMethodField()

    class Meta:
        model = Holiday
        fields = ["uid", "office", "is_global", "date", "name", "is_active", "notes", "created_at", "updated_at", "version"]
        read_only_fields = fields

    def get_is_global(self, holiday) -> bool:
        return holiday.office_id is None


class HolidayCreateSerializer(serializers.Serializer):
    office = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="Office uid; empty for every office.")
    date = serializers.DateField()
    name = serializers.CharField(max_length=120)
    is_active = serializers.BooleanField(required=False, default=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def to_representation(self, instance):
        return HolidaySerializer(holidays_queryset().get(pk=instance.pk), context=self.context).data


class HolidayUpdateSerializer(ExpectedVersionMixin, HolidayCreateSerializer):
    def get_fields(self):
        return _all_optional(super().get_fields())


# ----------------------------------------------------------------------------------------------------------------
# Leave types
# ----------------------------------------------------------------------------------------------------------------
class LeaveTypeSerializer(serializers.ModelSerializer):
    record_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = LeaveType
        fields = ["uid", "code", "name", "paid", "requires_approval", "record_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class LeaveTypeCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=40)
    name = serializers.CharField(max_length=120)
    paid = serializers.BooleanField(required=False, default=True)
    requires_approval = serializers.BooleanField(required=False, default=True)

    def to_representation(self, instance):
        return LeaveTypeSerializer(leave_types_queryset().get(pk=instance.pk), context=self.context).data


class LeaveTypeUpdateSerializer(ExpectedVersionMixin, LeaveTypeCreateSerializer):
    def get_fields(self):
        return _all_optional(super().get_fields())


# ----------------------------------------------------------------------------------------------------------------
# Leave records
# ----------------------------------------------------------------------------------------------------------------
class LeaveRecordSerializer(serializers.ModelSerializer):
    employee = EmployeeRefSerializer(read_only=True)
    leave_type = LeaveTypeRefSerializer(read_only=True)
    decided_by = HrActorRefSerializer(read_only=True, allow_null=True)
    days = serializers.FloatField(read_only=True, help_text="Calendar days (0.5 for a half day).")

    class Meta:
        model = LeaveRecord
        fields = ["uid", "employee", "leave_type", "date_from", "date_to", "days", "is_half_day", "status", "reason", "decided_by", "decided_at", "created_at", "updated_at", "version"]
        read_only_fields = fields


class LeaveCreateSerializer(serializers.Serializer):
    employee = serializers.SlugRelatedField(slug_field="uid", queryset=Employee.objects.all(), required=False, allow_null=True, default=None, help_text="Employee uid; empty: your own record.")
    leave_type = serializers.SlugRelatedField(slug_field="uid", queryset=LeaveType.objects.all(), help_text="Leave type uid.")
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    is_half_day = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")

    def to_representation(self, instance):
        return LeaveRecordSerializer(leave_queryset().get(pk=instance.pk), context=self.context).data


class LeaveActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="", help_text="Recorded in the audit log (e.g. the rejection reason).")


# ----------------------------------------------------------------------------------------------------------------
# Attendance rules
# ----------------------------------------------------------------------------------------------------------------
class RuleParamsSerializer(serializers.Serializer):
    """The recognised half-day keys; anything else is refused."""

    half_day_after = serializers.CharField(required=False, help_text="Clock time HH:MM; an arrival after it makes a half day (wins over the minutes).")
    half_day_after_minutes = serializers.IntegerField(required=False, min_value=0, max_value=MINUTE_LIMITS["half_day_after_minutes"], help_text="Minutes after the shift start.")
    half_day_under_minutes = serializers.IntegerField(
        required=False, min_value=0, max_value=MINUTE_LIMITS["half_day_under_minutes"], help_text="With a late arrival, fewer worked minutes make a half day."
    )

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(RECOGNISED_KEYS))
            if unknown:
                raise serializers.ValidationError({key: [f"Unknown key; use {', '.join(RECOGNISED_KEYS)}."] for key in unknown})
            if not data:
                raise serializers.ValidationError([f"State at least one of {', '.join(RECOGNISED_KEYS)}."])
        return super().to_internal_value(data)


class AttendanceRuleSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)
    shift = ShiftRefSerializer(read_only=True, allow_null=True)
    scope = serializers.ChoiceField(choices=AttendanceRule.Scope.choices, read_only=True)
    rules = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceRule
        fields = ["uid", "name", "scope", "office", "shift", "rules", "effective_from", "is_active", "notes", "created_at", "updated_at", "version"]
        read_only_fields = fields

    @extend_schema_field(RuleParamsSerializer)
    def get_rules(self, rule) -> dict:
        return dict(rule.rules or {})


class AttendanceRuleCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    office = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="Office uid (or shift, or neither = global).")
    shift = serializers.SlugRelatedField(slug_field="uid", queryset=Shift.objects.all(), required=False, allow_null=True, default=None, help_text="Shift uid.")
    rules = RuleParamsSerializer()
    effective_from = serializers.DateField(required=False, allow_null=True, default=None)
    is_active = serializers.BooleanField(required=False, default=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def to_representation(self, instance):
        return AttendanceRuleSerializer(rules_queryset().get(pk=instance.pk), context=self.context).data


class AttendanceRuleUpdateSerializer(ExpectedVersionMixin, AttendanceRuleCreateSerializer):
    def get_fields(self):
        return _all_optional(super().get_fields())
