"""Dashboard counters (``GET dashboard/``) for ``employees``, ``leave`` and ``hr_setup`` — record scope applied."""

from __future__ import annotations

from django.db.models import Count, Q

from core import dashboard, scopes
from hr.models import Employee, LeaveRecord, Office, Shift
from hr.services.common import today_in


@dashboard.register("employees")
def employee_counts(user) -> dict[str, int]:
    counts = scopes.apply(Employee.objects.all(), user, "employees").aggregate(active=Count("id", filter=Q(is_active=True)), inactive=Count("id", filter=Q(is_active=False)))
    return {"active": counts["active"], "inactive": counts["inactive"]}


@dashboard.register("leave")
def leave_counts(user) -> dict[str, int]:
    today = today_in()
    visible = scopes.apply(LeaveRecord.objects.filter(employee__deleted_at__isnull=True), user, "leave")
    counts = visible.aggregate(
        pending=Count("id", filter=Q(status=LeaveRecord.Status.PENDING)),
        on_leave_today=Count("employee", filter=Q(status=LeaveRecord.Status.APPROVED, date_from__lte=today, date_to__gte=today), distinct=True),
    )
    return {"pending": counts["pending"], "on_leave_today": counts["on_leave_today"]}


@dashboard.register("hr_setup")
def setup_counts(user) -> dict[str, int]:
    return {"offices": Office.objects.filter(is_active=True).count(), "shifts": Shift.objects.filter(is_active=True).count()}
