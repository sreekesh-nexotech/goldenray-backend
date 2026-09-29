"""List filters for ``site-inspections/`` and ``engineer/site-inspections/`` (``?field=`` or ``?filter[field]=``)."""

import django_filters

from site_inspections.models import Inspection
from site_inspections.models.choices import Complexity, Origin, Status, SystemType


class InspectionFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=Status.choices)
    system_type = django_filters.MultipleChoiceFilter(choices=SystemType.choices)
    origin = django_filters.ChoiceFilter(choices=Origin.choices)
    complexity_status = django_filters.ChoiceFilter(choices=Complexity.choices)
    engineer = django_filters.UUIDFilter(field_name="engineer__uid", help_text="Assigned engineer (user) uid.")
    unassigned = django_filters.BooleanFilter(field_name="engineer", lookup_expr="isnull", help_text="true: no engineer assigned.")
    customer = django_filters.UUIDFilter(field_name="customer__uid", help_text="Customer uid.")
    agreement = django_filters.UUIDFilter(field_name="agreement_uid", help_text="Agreement uid.")
    visit_from = django_filters.DateFilter(field_name="visit_date", lookup_expr="gte")
    visit_to = django_filters.DateFilter(field_name="visit_date", lookup_expr="lte")

    class Meta:
        model = Inspection
        fields: list[str] = []
