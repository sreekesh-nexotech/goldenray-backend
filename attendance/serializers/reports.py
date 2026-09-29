"""Report queries and responses (JSON, or the render job of a PDF / a report over 5,000 rows)."""

from __future__ import annotations

from rest_framework import serializers

from attendance.serializers.common import STATUS_CHOICES, MonthQuerySerializer
from documents.serializers.jobs import RenderJobSerializer

FORMATS = [("json", "json"), ("csv", "csv"), ("xlsx", "xlsx"), ("pdf", "pdf")]


class ReportBaseQuerySerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=FORMATS, required=False, default="json", help_text="`pdf` (and any report over 5,000 rows) answers 202 with a render job.")
    office = serializers.UUIDField(required=False, help_text="Office uid.")
    include_inactive = serializers.BooleanField(required=False, default=False, help_text="Also cover deactivated employees (a named employee is always covered).")


class DailyQuerySerializer(ReportBaseQuerySerializer):
    day = serializers.DateField(required=False, help_text="Default: today in the office's zone.")
    employee = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(choices=STATUS_CHOICES, required=False)


class WeeklyQuerySerializer(ReportBaseQuerySerializer):
    week_start = serializers.DateField()


class MonthlyQuerySerializer(ReportBaseQuerySerializer, MonthQuerySerializer):
    employee = serializers.UUIDField(required=False)


class RangeReportQuerySerializer(ReportBaseQuerySerializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    employee = serializers.UUIDField(required=False)


class ReportColumnSerializer(serializers.Serializer):
    key = serializers.CharField()
    header = serializers.CharField()


class ReportSerializer(serializers.Serializer):
    report = serializers.CharField()
    title = serializers.CharField()
    subtitle = serializers.CharField()
    columns = ReportColumnSerializer(many=True)
    rows = serializers.ListField(child=serializers.DictField())
    totals = serializers.DictField()
    row_count = serializers.IntegerField()


class ReportJobResponseSerializer(serializers.Serializer):
    report = serializers.CharField()
    format = serializers.CharField(help_text="Always `pdf`: large reports and PDFs are rendered by the documents worker.")
    requested_format = serializers.CharField()
    rows = serializers.IntegerField()
    message = serializers.CharField()
    job = RenderJobSerializer()
