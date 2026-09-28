"""HR URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``hr/`` — ``hr/offices/``, ``hr/shifts/``, ``hr/employees/``, ``hr/holidays/``, ``hr/leave-types/``,
``hr/leave/``, ``hr/attendance-rules/``.
"""

from rest_framework.routers import SimpleRouter

from hr.views.calendar import AttendanceRuleViewSet, HolidayViewSet, LeaveTypeViewSet, LeaveViewSet
from hr.views.employees import EmployeeViewSet
from hr.views.setup import OfficeViewSet, ShiftViewSet

router = SimpleRouter(trailing_slash=True)
router.register("hr/offices", OfficeViewSet, basename="hr-offices")
router.register("hr/shifts", ShiftViewSet, basename="hr-shifts")
router.register("hr/employees", EmployeeViewSet, basename="hr-employees")
router.register("hr/holidays", HolidayViewSet, basename="hr-holidays")
router.register("hr/leave-types", LeaveTypeViewSet, basename="hr-leave-types")
router.register("hr/leave", LeaveViewSet, basename="hr-leave")
router.register("hr/attendance-rules", AttendanceRuleViewSet, basename="hr-attendance-rules")

staff_urlpatterns = [*router.urls]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
