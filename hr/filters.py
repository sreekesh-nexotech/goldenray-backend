"""Staff list filters (``?field=`` or ``?filter[field]=``)."""

import django_filters
from django.db.models import Q

from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, Office, Shift


class OfficeFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Office
        fields: list[str] = []


class ShiftFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    is_overnight = django_filters.BooleanFilter()

    class Meta:
        model = Shift
        fields: list[str] = []


class EmployeeFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(field_name="office__uid", help_text="Office uid.")
    shift = django_filters.UUIDFilter(field_name="shift__uid", help_text="Own shift uid.")
    is_active = django_filters.BooleanFilter(help_text="Default: active only (eSSL behaviour); false lists the deactivated.")
    include_inactive = django_filters.BooleanFilter(method="filter_include_inactive", help_text="List everybody.")
    has_login = django_filters.BooleanFilter(field_name="user", lookup_expr="isnull", exclude=True, help_text="Linked to a Studio login.")
    identity_method = django_filters.ChoiceFilter(choices=Employee.IdentityMethod.choices)
    department = django_filters.CharFilter(field_name="department", lookup_expr="iexact")

    class Meta:
        model = Employee
        fields: list[str] = []

    def filter_include_inactive(self, queryset, name, value):
        return queryset  # handled with the is_active default in EmployeeViewSet.base_queryset


class HolidayFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(method="filter_office", help_text="Office uid: that office's holidays plus the global ones.")
    is_global = django_filters.BooleanFilter(field_name="office", lookup_expr="isnull")
    is_active = django_filters.BooleanFilter()
    year = django_filters.NumberFilter(field_name="date", lookup_expr="year")
    date_from = django_filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="date", lookup_expr="lte")

    class Meta:
        model = Holiday
        fields: list[str] = []

    def filter_office(self, queryset, name, value):
        return queryset.filter(Q(office__uid=value) | Q(office__isnull=True))


class LeaveFilter(django_filters.FilterSet):
    employee = django_filters.UUIDFilter(field_name="employee__uid", help_text="Employee uid.")
    office = django_filters.UUIDFilter(field_name="employee__office__uid", help_text="Office uid.")
    leave_type = django_filters.UUIDFilter(field_name="leave_type__uid", help_text="Leave type uid.")
    status = django_filters.ChoiceFilter(choices=LeaveRecord.Status.choices)
    date_from = django_filters.DateFilter(field_name="date_to", lookup_expr="gte", help_text="Leave ending on or after this date.")
    date_to = django_filters.DateFilter(field_name="date_from", lookup_expr="lte", help_text="Leave starting on or before this date.")

    class Meta:
        model = LeaveRecord
        fields: list[str] = []


class AttendanceRuleFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(field_name="office__uid")
    shift = django_filters.UUIDFilter(field_name="shift__uid")
    is_active = django_filters.BooleanFilter()
    scope = django_filters.ChoiceFilter(choices=AttendanceRule.Scope.choices, method="filter_scope")

    class Meta:
        model = AttendanceRule
        fields: list[str] = []

    def filter_scope(self, queryset, name, value):
        if value == "SHIFT":
            return queryset.filter(shift__isnull=False)
        if value == "OFFICE":
            return queryset.filter(office__isnull=False)
        return queryset.filter(office__isnull=True, shift__isnull=True)
