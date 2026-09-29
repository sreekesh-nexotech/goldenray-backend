"""``attendance/dashboard/summary/``, ``…/recent-punches/``, ``…/trend/`` (module ``attendance``: view) — scoped to the
caller (eSSL §I.1: its dashboard showed everyone's punches and every terminal's address to any login)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from attendance.serializers.dashboard import DashboardSummarySerializer, RecentPunchSerializer, RecentQuerySerializer, SummaryQuerySerializer, TrendPointSerializer, TrendQuerySerializer
from attendance.services import dashboard
from attendance.views.calendar import AttendanceReadView

from .days import READ_ERRORS, TAGS


class DashboardSummaryView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_dashboard_summary",
        parameters=[SummaryQuerySerializer],
        responses={200: DashboardSummarySerializer, **READ_ERRORS},
        tags=TAGS,
        description="The day's counts for the people in scope, per office (today: from the punches so far); terminal blocks only with devices.view.",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(SummaryQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        return Response(DashboardSummarySerializer(dashboard.summary(request.user, day=query.get("day"), office=office)).data)


class DashboardRecentPunchesView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_dashboard_recent_punches",
        parameters=[RecentQuerySerializer],
        responses={200: RecentPunchSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="The newest punches within scope (at most 200), with the person each PIN is linked to on its terminal.",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(RecentQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        rows = dashboard.recent_punches(request.user, limit=query["limit"], office=office)
        people = {(row["punch"].device_id, row["punch"].pin): row["employee"] for row in rows if row["employee"] is not None}
        return Response(RecentPunchSerializer(rows, many=True, context={"people": people}).data)


class DashboardTrendView(AttendanceReadView):
    @extend_schema(
        operation_id="attendance_dashboard_trend",
        parameters=[TrendQuerySerializer],
        responses={200: TrendPointSerializer(many=True), **READ_ERRORS},
        tags=TAGS,
        description="Daily counts for the last `days` days (1–90) up to today on the office's clock; today and later are pending, never absent.",
    )
    def get(self, request, *args, **kwargs):
        query = self.query(TrendQuerySerializer)
        office = dashboard.office_or_none(query.get("office"))
        return Response(TrendPointSerializer(dashboard.trend(request.user, days_back=query["days"], office=office), many=True).data)
