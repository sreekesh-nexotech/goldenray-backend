"""Calendars, rosters, the day view and the office-local date ranges — all drawn from the one calendar fill (A9)."""

from __future__ import annotations

from rest_framework import serializers

from attendance.serializers.common import STATUS_CHOICES, STATUS_CODE_CHOICES, EmployeeRefSerializer, InputDateField, MonthQuerySerializer, ShiftRefSerializer
from engines import attendance as engine

FILL_CHOICES = [(value, value) for value in (*engine.Fill, "PROVISIONAL")]
STATUS_OR_BLANK = [("", "")] + list(STATUS_CHOICES)


class CalendarDaySerializer(serializers.Serializer):
    """One date for one person. ``fill``: STORED (a processed day), FILLED (a past day without one: leave > holiday >
    weekly off > absent), PENDING (today or later: blank, never absent, A7), NOT_EMPLOYED (blank), PROVISIONAL (today
    from the punches so far; not stored)."""

    date = serializers.CharField()
    day = serializers.CharField()
    status = serializers.ChoiceField(choices=STATUS_OR_BLANK)
    status_code = serializers.ChoiceField(choices=[("", "")] + STATUS_CODE_CHOICES)
    status_label = serializers.CharField()
    fill = serializers.ChoiceField(choices=FILL_CHOICES)
    stored = serializers.BooleanField()
    first_in = serializers.CharField(help_text="HH:MM on the office wall clock, or blank.")
    last_out = serializers.CharField(help_text="HH:MM, `Missing OUT` when IN has no OUT, or blank.")
    missing_out = serializers.BooleanField()
    punch_count = serializers.IntegerField()
    working_minutes = serializers.IntegerField()
    working_hours = serializers.CharField()
    overtime_minutes = serializers.IntegerField()
    late_minutes = serializers.IntegerField()
    early_exit_minutes = serializers.IntegerField()
    worked_on_off_day = serializers.BooleanField()
    leave_conflict = serializers.BooleanField()
    is_corrected = serializers.BooleanField()
    holiday_name = serializers.CharField(allow_null=True)
    leave_type = serializers.CharField(allow_null=True)

    def to_representation(self, instance):
        return super().to_representation(instance.as_dict() if isinstance(instance, engine.CalendarDay) else instance)


class CalendarSummarySerializer(serializers.Serializer):
    present = serializers.IntegerField()
    late = serializers.IntegerField()
    absent = serializers.IntegerField()
    half_day = serializers.IntegerField()
    weekly_off = serializers.IntegerField()
    holiday = serializers.IntegerField()
    leave = serializers.IntegerField()
    present_days = serializers.IntegerField(help_text="Present + late + half day (a half day is a late arrival, not an absence).")
    absent_days = serializers.IntegerField()
    expected_days = serializers.IntegerField(help_text="Present days + absent days (leave, holidays, weekly offs are not expected).")
    worked_off_days = serializers.IntegerField()
    leave_conflicts = serializers.IntegerField()
    pending_days = serializers.IntegerField()
    not_employed_days = serializers.IntegerField()
    working_minutes = serializers.IntegerField()
    overtime_minutes = serializers.IntegerField()
    working_hours = serializers.CharField()
    overtime_hours = serializers.CharField()
    attendance_rate = serializers.DecimalField(max_digits=5, decimal_places=4, allow_null=True, help_text="A fraction: (present + late + 0.5 × half day) / expected days (A9).")


class CalendarQuerySerializer(MonthQuerySerializer):
    employee = serializers.UUIDField(help_text="Employee uid.")


class CalendarSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    shift = ShiftRefSerializer(allow_null=True)
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    month_label = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    days = CalendarDaySerializer(many=True)
    summary = CalendarSummarySerializer()


class RosterQuerySerializer(MonthQuerySerializer):
    office = serializers.UUIDField(required=False)
    employee = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, max_length=100)
    include_inactive = serializers.BooleanField(required=False, default=False)


class RosterRowSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    days = CalendarDaySerializer(many=True)
    summary = CalendarSummarySerializer()


class RosterDateSerializer(serializers.Serializer):
    date = serializers.CharField()
    day = serializers.CharField()
    day_number = serializers.IntegerField()


class RosterTotalsByDateSerializer(serializers.Serializer):
    date = serializers.CharField()
    day = serializers.CharField()
    present = serializers.IntegerField()
    late = serializers.IntegerField()
    absent = serializers.IntegerField()
    half_day = serializers.IntegerField()
    weekly_off = serializers.IntegerField()
    holiday = serializers.IntegerField()
    on_leave = serializers.IntegerField()
    stored = serializers.IntegerField()


class RosterTotalsSerializer(CalendarSummarySerializer):
    employees = serializers.IntegerField()
    days = serializers.IntegerField()


class RosterSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    month_label = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    dates = RosterDateSerializer(many=True)
    employees = RosterRowSerializer(many=True)
    by_date = RosterTotalsByDateSerializer(many=True)
    totals = RosterTotalsSerializer()


class DayQuerySerializer(serializers.Serializer):
    day = InputDateField(required=False, help_text="Default: today in the office's zone (the caller's office without `office`).")
    office = serializers.UUIDField(required=False)
    employee = serializers.UUIDField(required=False)
    search = serializers.CharField(required=False, max_length=100)
    include_inactive = serializers.BooleanField(required=False, default=False)


class StatusCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    present = serializers.IntegerField()
    late = serializers.IntegerField()
    absent = serializers.IntegerField()
    half_day = serializers.IntegerField()
    weekly_off = serializers.IntegerField()
    holiday = serializers.IntegerField()
    on_leave = serializers.IntegerField()
    pending = serializers.IntegerField(help_text="Blank days (today with no punch yet, or not final).")
    present_days = serializers.IntegerField()


class DayRosterRowSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    shift = ShiftRefSerializer(allow_null=True)
    cell = CalendarDaySerializer()
    day_uid = serializers.UUIDField(allow_null=True, help_text="The stored day (for corrections), when there is one.")


class DayRosterSerializer(serializers.Serializer):
    date = serializers.DateField()
    day_name = serializers.CharField()
    counts = StatusCountsSerializer()
    rows_without_stored_record = serializers.IntegerField()
    rows = DayRosterRowSerializer(many=True)


class DateRangeSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()


class DateRangesRangesSerializer(serializers.Serializer):
    today = DateRangeSerializer()
    yesterday = DateRangeSerializer()
    last7 = DateRangeSerializer()
    last30 = DateRangeSerializer()


class DateRangesSerializer(serializers.Serializer):
    office = serializers.UUIDField(allow_null=True)
    timezone = serializers.CharField()
    today = serializers.DateField()
    ranges = DateRangesRangesSerializer()
