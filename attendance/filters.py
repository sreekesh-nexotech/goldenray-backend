"""Staff list filters (``?field=`` or ``?filter[field]=``)."""

import django_filters

from attendance.models import AttendanceCorrection, AttendanceDay


class AttendanceDayFilter(django_filters.FilterSet):
    date_from = django_filters.DateFilter(field_name="work_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="work_date", lookup_expr="lte")
    employee = django_filters.UUIDFilter(field_name="employee__uid", help_text="Employee uid.")
    office = django_filters.UUIDFilter(field_name="office__uid", help_text="Office uid (the posting office when the day was computed).")
    status = django_filters.ChoiceFilter(choices=AttendanceDay.Status.choices)
    is_corrected = django_filters.BooleanFilter()
    missing_out = django_filters.BooleanFilter()
    include_inactive = django_filters.BooleanFilter(method="filter_include_inactive", help_text="Also list deactivated employees' days (default: active employees only).")

    class Meta:
        model = AttendanceDay
        fields: list[str] = []

    def filter_include_inactive(self, queryset, name, value):
        return queryset  # the active-only default is applied in the view (it needs the flag's absence too)


class AttendanceCorrectionFilter(django_filters.FilterSet):
    employee = django_filters.UUIDFilter(field_name="day__employee__uid", help_text="Employee uid.")
    day = django_filters.UUIDFilter(field_name="day__uid", help_text="Attendance day uid.")
    active = django_filters.BooleanFilter(field_name="revoked_at", lookup_expr="isnull", help_text="true: not revoked.")
    date_from = django_filters.DateFilter(field_name="day__work_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="day__work_date", lookup_expr="lte")

    class Meta:
        model = AttendanceCorrection
        fields: list[str] = []
