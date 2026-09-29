"""``attendance/dashboard/`` responses."""

from __future__ import annotations

from rest_framework import serializers

from attendance.serializers.calendar import StatusCountsSerializer
from attendance.serializers.common import InputDateField, OfficeRefSerializer
from attendance.serializers.days import AttendanceRawPunchSerializer
from hr.serializers.refs import EmployeeRefSerializer


class SummaryQuerySerializer(serializers.Serializer):
    day = InputDateField(required=False, help_text="Default: today in the office's zone (the caller's office without `office`).")
    office = serializers.UUIDField(required=False)


class OverallCountsSerializer(StatusCountsSerializer):
    currently_in = serializers.IntegerField(help_text="Today: people with an IN and no OUT yet.")
    punches = serializers.IntegerField()


class OfficeBlockSerializer(serializers.Serializer):
    office = OfficeRefSerializer()
    today = serializers.DateField(help_text="Today on this office's clock.")
    counts = StatusCountsSerializer()
    punches = serializers.IntegerField(help_text="Raw punches on the day from this office's terminals (within scope).")
    devices = serializers.JSONField(required=False, help_text="The office's connection and terminals (callers with devices.view only).")


class DashboardSummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    scope = serializers.CharField(allow_null=True)
    provisional = serializers.BooleanField(help_text="Today's figures come from the punches so far; nothing is stored before the day is final (A7).")
    overall = OverallCountsSerializer()
    offices = OfficeBlockSerializer(many=True)


class RecentQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, default=20, min_value=1, max_value=200)
    office = serializers.UUIDField(required=False)


class RecentPunchSerializer(serializers.Serializer):
    punch = AttendanceRawPunchSerializer()
    employee = EmployeeRefSerializer(allow_null=True)


class TrendQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(required=False, default=7, min_value=1, max_value=90)
    office = serializers.UUIDField(required=False)


class TrendPointSerializer(StatusCountsSerializer):
    date = serializers.DateField()
