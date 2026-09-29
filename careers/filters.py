"""Staff list filters (``?field=`` or ``?filter[field]=``)."""

import django_filters

from careers.models import Department, JobApplication, JobPosition


class DepartmentFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Department
        fields: list[str] = []


class JobPositionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=JobPosition.Status.choices)
    department = django_filters.UUIDFilter(field_name="department__uid", help_text="Department uid.")
    employment_type = django_filters.ChoiceFilter(choices=JobPosition.EmploymentType.choices)
    location = django_filters.CharFilter(field_name="location", lookup_expr="icontains")

    class Meta:
        model = JobPosition
        fields: list[str] = []


class JobApplicationFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=JobApplication.Status.choices)
    position = django_filters.UUIDFilter(field_name="position__uid", help_text="Posting uid.")
    assignee = django_filters.UUIDFilter(field_name="assignee__uid", help_text="User uid.")
    source = django_filters.ChoiceFilter(choices=JobApplication.Source.choices)
    created_from = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = JobApplication
        fields: list[str] = []
