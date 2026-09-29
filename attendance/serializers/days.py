"""Processed days, raw punches and the day timeline."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from attendance.models import AttendanceDay, RawPunch
from attendance.serializers.common import (
    STATUS_CHOICES,
    STATUS_CODE_CHOICES,
    AttendanceDeviceRefSerializer,
    EmployeeRefSerializer,
    OfficeRefSerializer,
    OriginSerializer,
    ShiftRefSerializer,
    WallClockField,
)
from engines import attendance as engine


class AttendanceDaySerializer(serializers.ModelSerializer):
    employee = EmployeeRefSerializer(read_only=True)
    office = OfficeRefSerializer(read_only=True, allow_null=True, help_text="Posting office when the day was computed.")
    shift = ShiftRefSerializer(read_only=True, allow_null=True)
    first_device = AttendanceDeviceRefSerializer(read_only=True, allow_null=True, help_text="Terminal of the first accepted punch.")
    first_in = WallClockField()
    last_out = WallClockField(help_text="Null with `missing_out` when the closing punch never came (never invented).")
    status = serializers.ChoiceField(choices=STATUS_CHOICES, read_only=True)
    status_code = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()
    origin = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceDay
        fields = [
            "uid",
            "employee",
            "work_date",
            "office",
            "shift",
            "first_device",
            "first_in",
            "last_out",
            "missing_out",
            "punch_count",
            "working_minutes",
            "break_minutes",
            "late_minutes",
            "early_exit_minutes",
            "overtime_minutes",
            "is_late",
            "is_early_exit",
            "status",
            "status_code",
            "status_label",
            "worked_on_off_day",
            "leave_conflict",
            "is_corrected",
            "processing_version",
            "computed_at",
            "origin",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=STATUS_CODE_CHOICES))
    def get_status_code(self, day) -> str:
        return engine.STATUS_CODE.get(day.status, "")

    @extend_schema_field(OpenApiTypes.STR)
    def get_status_label(self, day) -> str:
        return engine.STATUS_LABEL.get(day.status, "")

    @extend_schema_field(OriginSerializer(allow_null=True))
    def get_origin(self, day):
        return (self.context.get("origins") or {}).get(day.pk)


class AttendanceRawPunchSerializer(serializers.ModelSerializer):
    device = AttendanceDeviceRefSerializer(read_only=True)
    device_time = WallClockField(allow_null=False, help_text="The terminal's own clock, as reported (never corrected).")
    employee = serializers.SerializerMethodField()

    class Meta:
        model = RawPunch
        fields = ["dedup_key", "device", "device_serial", "device_record_uid", "pin", "device_time", "punch_at", "status_code", "punch_code", "source", "received_at", "employee"]
        read_only_fields = fields

    @extend_schema_field(EmployeeRefSerializer(allow_null=True))
    def get_employee(self, punch):
        employee = (self.context.get("people") or {}).get((punch.device_id, punch.pin))
        return EmployeeRefSerializer(employee).data if employee is not None else None


class RawQuerySerializer(serializers.Serializer):
    device = serializers.UUIDField(required=False, help_text="Device uid.")
    pin = serializers.CharField(required=False, max_length=80)
    employee = serializers.UUIDField(required=False, help_text="Employee uid: the punches of their per-device PIN links (A1).")
    date_from = serializers.DateField(required=False, help_text="Terminal-local date.")
    date_to = serializers.DateField(required=False)


class TimelineQuerySerializer(serializers.Serializer):
    work_date = serializers.DateField()


class AttendanceTimelinePunchSerializer(serializers.Serializer):
    punch = AttendanceRawPunchSerializer()
    office_time = WallClockField(allow_null=False, help_text="The punch on the employee's office wall clock.")
    work_date = serializers.DateField(help_text="The work date the punch belongs to (overnight buffer, A6).")
    accepted = serializers.BooleanField(help_text="IN, OUT or an intermediate punch of the day.")
    ignored = serializers.BooleanField(help_text="A double scan within the shift's debounce window (A3).")
    in_day = serializers.BooleanField()


class AttendanceTimelineSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    work_date = serializers.DateField()
    timezone = serializers.CharField()
    day = AttendanceDaySerializer(allow_null=True)
    punches = AttendanceTimelinePunchSerializer(many=True)
