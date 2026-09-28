"""Office and shift shapes (staff ``hr/offices/``, ``hr/shifts/``)."""

from __future__ import annotations

from datetime import datetime, timedelta

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from hr.models import AttendanceRule, Office, Shift
from hr.models.shift import MINUTE_FIELDS
from hr.serializers.refs import OfficeRefSerializer, ShiftRefSerializer
from hr.services.common import today_in
from hr.services.offices import offices_queryset
from hr.services.rules import applicable_rules, merged, parse_clock
from hr.services.shifts import shifts_queryset


# ----------------------------------------------------------------------------------------------------------------
# Shifts
# ----------------------------------------------------------------------------------------------------------------
class ShiftSerializer(serializers.ModelSerializer):
    employee_count = serializers.IntegerField(read_only=True, default=0, help_text="Active employees with this as their own shift.")
    half_day_after = serializers.SerializerMethodField(help_text="The arrival deadline in force today (global and shift rules applied).")
    half_day_after_source = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = [
            "uid",
            "code",
            "name",
            "start_time",
            "end_time",
            "is_overnight",
            *[name for name, _, _ in MINUTE_FIELDS],
            "auto_deduct_break",
            "overtime_enabled",
            "working_days",
            "weekly_off_days",
            "is_active",
            "employee_count",
            "half_day_after",
            "half_day_after_source",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def _deadline(self, shift: Shift) -> tuple[str, str]:
        cache = self.context.setdefault("_hr_deadlines", {})
        if shift.pk not in cache:
            if "_hr_active_rules" not in self.context:  # one query for the whole list (the context is shared)
                self.context["_hr_active_rules"] = list(AttendanceRule.objects.filter(is_active=True))
            active = self.context["_hr_active_rules"]
            today = today_in()
            rules = merged(rule for rule in applicable_rules(shift=shift, on=today, candidates=active) if rule.office_id is None)
            start = datetime.combine(today, shift.start_time)
            if rules.get("half_day_after"):
                cache[shift.pk] = (parse_clock(rules["half_day_after"]).strftime("%H:%M:%S"), "rule")
            elif "half_day_after_minutes" in rules:
                cache[shift.pk] = ((start + timedelta(minutes=rules["half_day_after_minutes"])).strftime("%H:%M:%S"), "rule")
            else:
                cache[shift.pk] = ((start + timedelta(minutes=shift.half_day_after_minutes)).strftime("%H:%M:%S"), "shift")
        return cache[shift.pk]

    @extend_schema_field(serializers.TimeField())
    def get_half_day_after(self, shift) -> str:
        return self._deadline(shift)[0]

    @extend_schema_field(serializers.ChoiceField(choices=Shift.HalfDayAfterSource.choices))
    def get_half_day_after_source(self, shift) -> str:
        return self._deadline(shift)[1]


class ShiftCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    name = serializers.CharField(max_length=120)
    start_time = serializers.TimeField()
    end_time = serializers.TimeField(help_text="On the next day when is_overnight.")
    is_overnight = serializers.BooleanField(required=False, default=False)
    auto_deduct_break = serializers.BooleanField(required=False, default=True)
    overtime_enabled = serializers.BooleanField(required=False, default=True)
    working_days = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), required=False, help_text="Weekdays 0=Monday … 6=Sunday (default Monday–Saturday).")
    weekly_off_days = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), required=False, help_text="Weekdays; wins over working_days (default Sunday).")
    is_active = serializers.BooleanField(required=False, default=True)

    def get_fields(self):
        fields = super().get_fields()
        for name, default, maximum in MINUTE_FIELDS:
            fields[name] = serializers.IntegerField(min_value=0, max_value=maximum, required=False, default=default)
        return fields

    def to_representation(self, instance):
        return ShiftSerializer(shifts_queryset().get(pk=instance.pk), context=self.context).data


class ShiftUpdateSerializer(ExpectedVersionMixin, ShiftCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields


# ----------------------------------------------------------------------------------------------------------------
# Offices
# ----------------------------------------------------------------------------------------------------------------
class AttendanceSettingsSerializer(serializers.Serializer):
    """eSSL ``attendance_settings``: the office's default shift under the names the settings screen uses."""

    shift = ShiftRefSerializer()
    grace_minutes = serializers.IntegerField()
    late_threshold_minutes = serializers.IntegerField()
    half_day_minutes = serializers.IntegerField()
    full_day_minutes = serializers.IntegerField()
    break_minutes = serializers.IntegerField()
    auto_deduct_break = serializers.BooleanField()
    overtime_enabled = serializers.BooleanField()
    overtime_after_minutes = serializers.IntegerField()
    working_days = serializers.ListField(child=serializers.IntegerField())
    weekly_off_days = serializers.ListField(child=serializers.IntegerField())


class OfficeSerializer(serializers.ModelSerializer):
    default_shift = ShiftRefSerializer(read_only=True, allow_null=True)
    attendance_settings = serializers.SerializerMethodField()
    employee_count = serializers.IntegerField(read_only=True, default=0, help_text="Active employees.")

    class Meta:
        model = Office
        fields = ["uid", "code", "name", "address", "timezone", "default_shift", "attendance_settings", "is_active", "employee_count", "created_at", "updated_at", "version"]
        read_only_fields = fields

    @extend_schema_field(AttendanceSettingsSerializer(allow_null=True))
    def get_attendance_settings(self, office):
        shift = office.default_shift
        if shift is None:
            return None
        names = [name for name in AttendanceSettingsSerializer().fields if name != "shift"]
        return {"shift": ShiftRefSerializer(shift).data, **{name: getattr(shift, name) for name in names}}


class OfficeCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    name = serializers.CharField(max_length=150)
    address = serializers.CharField(required=False, allow_blank=True, default="")
    timezone = serializers.CharField(max_length=60, required=False, default="Asia/Kolkata", help_text="IANA zone, e.g. Asia/Kolkata.")
    default_shift = serializers.SlugRelatedField(slug_field="uid", queryset=Shift.objects.all(), required=False, allow_null=True, default=None, help_text="Shift uid.")
    is_active = serializers.BooleanField(required=False, default=True)

    def to_representation(self, instance):
        return OfficeSerializer(offices_queryset().get(pk=instance.pk), context=self.context).data


class OfficeUpdateSerializer(ExpectedVersionMixin, OfficeCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields


class _CountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()


class _LeaveCountsSerializer(serializers.Serializer):
    on_leave = serializers.IntegerField(help_text="Employees on approved leave that day.")
    pending = serializers.IntegerField(help_text="Pending requests covering that day.")


class _HolidayRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField()
    is_global = serializers.BooleanField()


class OfficeSummarySerializer(serializers.Serializer):
    office = OfficeRefSerializer()
    date = serializers.DateField()
    employees = _CountsSerializer()
    leave = _LeaveCountsSerializer()
    holiday = _HolidayRefSerializer(allow_null=True)
    is_weekly_off = serializers.BooleanField()
    is_working_day = serializers.BooleanField()
    sections = serializers.DictField(child=serializers.JSONField(), help_text="Sections other packages add (attendance counts, devices, agents …).")


class DayQuerySerializer(serializers.Serializer):
    day = serializers.DateField(required=False, help_text="Default: today in the office's time zone.")
