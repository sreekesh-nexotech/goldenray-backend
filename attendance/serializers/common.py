"""Shared fields and embedded references of the attendance responses."""

from __future__ import annotations

from datetime import datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from attendance.models import AttendanceDay
from attendance.models.day import CORRECTABLE_FIELDS
from devices.models import Device
from devices.services import health
from engines import attendance as engine
from hr.serializers.refs import EmployeeRefSerializer, OfficeRefSerializer, ShiftRefSerializer  # noqa: F401 - re-exported

STATUS_CHOICES = AttendanceDay.Status.choices
STATUS_CODE_CHOICES = [(code, code) for code in engine.STATUS_CODE.values()]
CORRECTION_FIELD_CHOICES = [(name, name) for name in CORRECTABLE_FIELDS]
TRANSPORT_CHOICES = [("AGENT", "Office agent"), ("ADMS", "ADMS push"), ("NONE", "No transport configured")]
WALL_CLOCK_FORMAT = "%Y-%m-%dT%H:%M:%S"


@extend_schema_field({"type": "string", "format": "date-time", "nullable": True, "example": "2026-09-01T09:31:05", "description": "Office wall-clock time, no zone."})
class WallClockField(serializers.Field):
    """A wall-clock reading (``timestamp``): ``YYYY-MM-DDTHH:MM:SS`` without a zone — never shifted into another one."""

    def __init__(self, **kwargs):
        kwargs.setdefault("read_only", True)
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)

    def to_representation(self, value):
        return value.strftime(WALL_CLOCK_FORMAT) if isinstance(value, datetime) else None


class AttendanceDeviceRefSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)
    display_label = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["uid", "name", "serial_number", "office", "display_label"]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_display_label(self, device) -> str:
        return health.display_label(device)


class OriginSerializer(serializers.Serializer):
    """Where a day's punches came from, read back from the punches themselves (never the one first-punch device)."""

    sources = serializers.ListField(child=serializers.CharField())
    devices = serializers.ListField(child=serializers.CharField())
    offices = serializers.ListField(child=serializers.CharField())


class RangeQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()

    def validate(self, attrs):
        if attrs["date_to"] < attrs["date_from"]:
            raise serializers.ValidationError({"date_to": "Must not be before date_from."})
        return attrs


class MonthQuerySerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)


class OfficeQuerySerializer(serializers.Serializer):
    office = serializers.UUIDField(required=False, help_text="Office uid.")
