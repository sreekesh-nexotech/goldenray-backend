"""List filters for ``projects/`` (``?field=`` or ``?filter[field]=``)."""

import django_filters

from projects.models import KsebStatus, Project, ProjectStatus, SystemType


class ProjectFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ProjectStatus.choices)
    system_type = django_filters.ChoiceFilter(choices=SystemType.choices)
    kseb_status = django_filters.ChoiceFilter(choices=KsebStatus.choices)
    customer = django_filters.UUIDFilter(field_name="customer__uid", help_text="Customer uid.")
    head = django_filters.UUIDFilter(field_name="head__uid", help_text="Project Head (user) uid.")
    site_inspection_uid = django_filters.UUIDFilter(field_name="site_inspection_uid")
    bom_locked = django_filters.BooleanFilter(field_name="bom_lock", lookup_expr="isnull", exclude=True, help_text="true: the BOM is locked.")
    created_from = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Project
        fields: list[str] = []
