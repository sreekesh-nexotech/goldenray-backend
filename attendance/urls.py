"""Attendance URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``attendance/``.
"""

from django.urls import path
from rest_framework.routers import SimpleRouter

from attendance.views.calendar import CalendarView, DateRangesView, DayView, RosterView
from attendance.views.dashboard import DashboardRecentPunchesView, DashboardSummaryView, DashboardTrendView
from attendance.views.days import AttendanceDayViewSet, RawPunchViewSet, TimelineView
from attendance.views.process import CorrectionViewSet, ProcessAllView, ProcessView, RecalculateView
from attendance.views.reports import (
    DailyReportView,
    IndividualReportView,
    MonthlyDetailReportView,
    MonthlyIndividualReportView,
    MonthlyReportView,
    OfficeReportView,
    WeeklyReportView,
)

router = SimpleRouter(trailing_slash=True)
router.register("attendance/days", AttendanceDayViewSet, basename="attendance-days")
router.register("attendance/raw", RawPunchViewSet, basename="attendance-raw")
router.register("attendance/corrections", CorrectionViewSet, basename="attendance-corrections")

staff_urlpatterns = [
    *router.urls,
    path("attendance/employees/<uuid:uid>/timeline/", TimelineView.as_view(), name="attendance-timeline"),
    path("attendance/calendar/", CalendarView.as_view(), name="attendance-calendar"),
    path("attendance/calendar/all/", RosterView.as_view(), name="attendance-calendar-all"),
    path("attendance/day/", DayView.as_view(), name="attendance-day"),
    path("attendance/date-ranges/", DateRangesView.as_view(), name="attendance-date-ranges"),
    path("attendance/process/", ProcessView.as_view(), name="attendance-process"),
    path("attendance/process-all/", ProcessAllView.as_view(), name="attendance-process-all"),
    path("attendance/recalculate/", RecalculateView.as_view(), name="attendance-recalculate"),
    path("attendance/reports/daily/", DailyReportView.as_view(), name="attendance-report-daily"),
    path("attendance/reports/weekly/", WeeklyReportView.as_view(), name="attendance-report-weekly"),
    path("attendance/reports/monthly/", MonthlyReportView.as_view(), name="attendance-report-monthly"),
    path("attendance/reports/monthly-detail/", MonthlyDetailReportView.as_view(), name="attendance-report-monthly-detail"),
    path("attendance/reports/monthly-individual/", MonthlyIndividualReportView.as_view(), name="attendance-report-monthly-individual"),
    path("attendance/reports/individual/", IndividualReportView.as_view(), name="attendance-report-individual"),
    path("attendance/reports/office/", OfficeReportView.as_view(), name="attendance-report-office"),
    path("attendance/dashboard/summary/", DashboardSummaryView.as_view(), name="attendance-dashboard-summary"),
    path("attendance/dashboard/recent-punches/", DashboardRecentPunchesView.as_view(), name="attendance-dashboard-recent-punches"),
    path("attendance/dashboard/trend/", DashboardTrendView.as_view(), name="attendance-dashboard-trend"),
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
