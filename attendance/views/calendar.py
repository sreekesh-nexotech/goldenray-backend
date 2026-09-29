"""``attendance/calendar/``, ``attendance/calendar/all/``, ``attendance/day/``, ``attendance/date-ranges/`` (module
``attendance``: view; the people are the caller's record scope). Every cell comes from the one calendar fill (A9);
today and later are blank, never absent (A7); ``day/`` shows today from the punches so far (provisional)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from attendance.serializers.calendar import (
    CalendarQuerySerializer,
    CalendarSerializer,
    DateRangesSerializer,
    DayQuerySerializer,
    DayRosterSerializer,
    RosterQuerySerializer,
    RosterSerializer,
)
from attendance.serializers.common import OfficeQuerySerializer
from attendance.services import calendar, dashboard, days, inputs
from attendance.services.common import today_for
from core.views import BaseAPIView
from engines import attendance as engine
from hr.models import Office

from .days import READ_ERRORS, TAGS


class AttendanceReadView(BaseAPIView):
    module = "attendance"
    action_permissions = {"GET": "view"}

    def query(self, serializer_class):
        query = serializer_class(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        return query.validated_data

    def people(self, *, office=None, employee=None, search=None, include_inactive=False):
        """The caller's employees (record scope), optionally one office / one person (a named person is always covered)."""
        queryset = dashboard.scoped_employees(self.request.user, office=office, include_inactive=include_inactive or employee is not None)
        if employee is not None:
            queryset = queryset.filter(uid=employee)
        return days.search_employees(queryset, search)


class CalendarView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_calendar", parameters=[CalendarQuerySerializer], responses={200: CalendarSerializer, **READ_ERRORS}, tags=TAGS, description="One person, one month, every date."
    )
    def get(self, request, *args, **kwargs):
        query = self.query(CalendarQuerySerializer)
        employee = days.employee_or_404(self.people(employee=query["employee"]), query["employee"])
        view = calendar.month_view(employee, query["year"], query["month"])
        return Response(CalendarSerializer({**view, "shift": inputs.effective_shift(employee)}).data)


class RosterView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_calendar_all",
        parameters=[RosterQuerySerializer],
        responses={200: RosterSerializer, **READ_ERRORS},
        tags=TAGS,
        description=f"Every employee's month on one calendar, with per-date totals. At most {calendar.MAX_ROSTER_EMPLOYEES} employees (400 `too_many_employees`; never truncated).",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(RosterQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        people = self.people(office=office, employee=query.get("employee"), search=query.get("search"), include_inactive=query["include_inactive"])
        return Response(RosterSerializer(calendar.roster(people, query["year"], query["month"])).data)


class DayView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_day",
        parameters=[DayQuerySerializer],
        responses={200: DayRosterSerializer, **READ_ERRORS},
        tags=TAGS,
        description="One day for everybody in scope, each with a stated reason. Final days come from the stored/filled calendar; today from the punches so far (`fill: PROVISIONAL`, nothing stored).",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(DayQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        people = list(self.people(office=office, employee=query.get("employee"), search=query.get("search"), include_inactive=query["include_inactive"]))
        day = query.get("day") or today_for(dashboard.reference_office(request.user, office))
        cells, stored = calendar.day_cells(people, day)
        rows = [
            {"employee": employee, "shift": inputs.effective_shift(employee), "cell": cells[employee.pk], "day_uid": stored[employee.pk].uid if stored.get(employee.pk) else None}
            for employee in people
        ]
        payload = {
            "date": day,
            "day_name": day.strftime("%A"),
            "counts": calendar.status_counts(cells.values()),
            "rows_without_stored_record": sum(1 for row in rows if row["day_uid"] is None and row["cell"].fill != engine.Fill.NOT_EMPLOYED),
            "rows": rows,
        }
        return Response(DayRosterSerializer(payload).data)


class DateRangesView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_date_ranges",
        parameters=[OfficeQuerySerializer],
        responses={200: DateRangesSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Today / yesterday / last 7 / last 30 days as dates on the office's clock (the caller's office without `office`).",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(OfficeQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        reference = dashboard.reference_office(request.user, office)
        data = calendar.date_ranges(reference)
        return Response(DateRangesSerializer({**data, "office": reference.uid if isinstance(reference, Office) else None}).data)
