"""Pages list filters (``?field=`` or ``?filter[field]=``)."""

import django_filters

from sitepages.models import Page


class PageFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=Page.Status.choices)
    group = django_filters.CharFilter(field_name="group", help_text="Exact group, e.g. `Solutions`.")
    template = django_filters.CharFilter(field_name="template")
    is_protected = django_filters.BooleanFilter(field_name="is_protected")
    verified = django_filters.BooleanFilter(field_name="verified_at", lookup_expr="isnull", exclude=True, help_text="true: reviewed since the last content change.")

    class Meta:
        model = Page
        fields: list[str] = []
