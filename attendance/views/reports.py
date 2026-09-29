"""``attendance/reports/{daily|weekly|monthly|monthly-detail|monthly-individual|individual|office}/?format=`` (module
``attendance``: export; the people are the caller's record scope). JSON by default; ``csv`` / ``xlsx`` files;
``pdf`` — and any report over 5,000 rows — answers 202 with the documents render job (``documents/jobs/<uid>/``)."""

from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response

from attendance.serializers.reports import DailyQuerySerializer, MonthlyQuerySerializer, RangeReportQuerySerializer, ReportJobResponseSerializer, ReportSerializer, WeeklyQuerySerializer
from attendance.services import dashboard, days, exporters, reports
from attendance.services.common import today_for
from attendance.views.calendar import AttendanceReadView
from devices.models import DeviceUser
from documents.serializers.jobs import RenderJobSerializer
from hr.models import Office

from .days import READ_ERRORS, TAGS

FILE = OpenApiResponse(response=OpenApiTypes.BINARY, description="`format=csv` (UTF-8 with BOM) or `format=xlsx`, as an attachment.")


def report_schema(name: str, query, description: str):
    return extend_schema(
        operation_id=f"attendance_reports_{name.replace('-', '_')}",
        parameters=[query],
        responses={200: OpenApiResponse(response=ReportSerializer, description="JSON (or a file, see `format`)."), 202: ReportJobResponseSerializer, **READ_ERRORS},
        tags=TAGS,
        description=description,
    )


class ReportNegotiation(DefaultContentNegotiation):
    """``?format=`` names the report file here, not a DRF renderer (DRF would answer 404 for ``csv``)."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class ReportView(AttendanceReadView):
    action_permissions = {"GET": "export"}
    content_negotiation_class = ReportNegotiation

    def respond(self, report, fmt: str):
        if exporters.needs_render_job(report, fmt):
            job = exporters.request_pdf(report, user=self.request.user, requested_format=fmt)
            message = (
                "The report is being rendered as a PDF."
                if fmt == "pdf"
                else f"More than {exporters.ASYNC_ROW_LIMIT} rows: the report is being rendered as a PDF. Narrow the selection for a {fmt} file."
            )
            payload = {"report": report.kind, "format": "pdf", "requested_format": fmt, "rows": len(report.rows), "message": message, "job": RenderJobSerializer(job).data}
            return Response(payload, status=status.HTTP_202_ACCEPTED)
        if fmt == "json":
            return Response(report.as_json())
        content, media_type, filename = exporters.render_file(report, fmt)
        response = HttpResponse(content, content_type=media_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        return response

    def scope(self, query):
        office = dashboard.office_or_none(query.get("office"))
        employee_uid = query.get("employee")
        people = self.people(office=office, employee=employee_uid, include_inactive=query.get("include_inactive", False))
        employee = days.employee_or_404(people, employee_uid) if employee_uid is not None else None
        return office, people, employee


def _pins(employee) -> list[str]:
    return sorted(DeviceUser.objects.filter(employee=employee).values_list("pin", flat=True).distinct())


class DailyReportView(ReportView):
    @report_schema("daily", DailyQuerySerializer, "One row per person in scope for a day (calendar-filled: a day without a stored row still says why).")
    def get(self, request, *args, **kwargs):
        query = self.query(DailyQuerySerializer)
        office, people, employee = self.scope(query)
        day = query.get("day") or today_for(dashboard.reference_office(request.user, office))
        report = reports.daily([employee] if employee else people, day, office=office, status=query.get("status"))
        return self.respond(report, query["format"])


class WeeklyReportView(ReportView):
    @report_schema("weekly", WeeklyQuerySerializer, "Seven day columns: H:MM worked, else the unambiguous status code (P LT A HD WO H L).")
    def get(self, request, *args, **kwargs):
        query = self.query(WeeklyQuerySerializer)
        office, people, _ = self.scope(query)
        return self.respond(reports.weekly(people, query["week_start"], office=office), query["format"])


class MonthlyReportView(ReportView):
    @report_schema("monthly", MonthlyQuerySerializer, "One row per person: the status counts, attendance % and hours of the month.")
    def get(self, request, *args, **kwargs):
        query = self.query(MonthlyQuerySerializer)
        office, people, employee = self.scope(query)
        return self.respond(reports.monthly([employee] if employee else people, query["year"], query["month"], office=office), query["format"])


class MonthlyDetailReportView(ReportView):
    @report_schema("monthly-detail", MonthlyQuerySerializer, "One row per person and date of the month.")
    def get(self, request, *args, **kwargs):
        query = self.query(MonthlyQuerySerializer)
        office, people, employee = self.scope(query)
        return self.respond(reports.monthly_detail([employee] if employee else people, query["year"], query["month"], office=office), query["format"])


class MonthlyIndividualReportView(ReportView):
    @report_schema("monthly-individual", MonthlyQuerySerializer, "One person's month with its summary; without `employee` the monthly detail of everyone in scope.")
    def get(self, request, *args, **kwargs):
        query = self.query(MonthlyQuerySerializer)
        office, people, employee = self.scope(query)
        if employee is None:
            return self.respond(reports.monthly_detail(people, query["year"], query["month"], office=office), query["format"])
        return self.respond(reports.monthly_individual(employee, query["year"], query["month"], pins=_pins(employee)), query["format"])


class IndividualReportView(ReportView):
    @report_schema("individual", RangeReportQuerySerializer, f"Day-by-day over a window (at most {reports.MAX_RANGE_DAYS} days), for one person or everyone in scope.")
    def get(self, request, *args, **kwargs):
        query = self.query(RangeReportQuerySerializer)
        office, people, employee = self.scope(query)
        report = reports.individual(people, query["date_from"], query["date_to"], employee=employee, office=office, pins=_pins(employee) if employee else None)
        return self.respond(report, query["format"])


class OfficeReportView(ReportView):
    @report_schema("office", RangeReportQuerySerializer, "One row per office in scope: expected days, present, absent, half days, leave, attendance % (one formula for every line, A9).")
    def get(self, request, *args, **kwargs):
        query = self.query(RangeReportQuerySerializer)
        office, people, _ = self.scope(query)
        people = [person for person in people.select_related("office") if person.office_id]
        offices = [office] if office is not None else sorted({person.office_id: person.office for person in people}.values(), key=lambda item: item.name)
        offices = [item for item in offices if isinstance(item, Office)]
        return self.respond(reports.office_report(people, offices, query["date_from"], query["date_to"]), query["format"])
