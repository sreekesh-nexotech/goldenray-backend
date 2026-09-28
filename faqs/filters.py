"""FAQ list filters (``?field=`` or ``?filter[field]=``).

``?page`` is the pagination parameter, so the page is filtered with ``page_uid`` (or ``route``). ``section`` filters
exactly whenever it is present — ``?section=`` selects the unnamed section — the same rule as the public endpoint.
Archived FAQs are left out of the working list unless ``status=ARCHIVED`` or ``include_archived=true`` (applied by
the view on the list action only, so detail and ``restore/`` still reach an archived FAQ).
"""

import django_filters

from faqs.models import Faq, FaqCategory


class FaqFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=Faq.Status.choices)
    page_uid = django_filters.UUIDFilter(field_name="page__uid")
    route = django_filters.CharFilter(field_name="page__route", help_text="Exact site path of the page.")
    section = django_filters.CharFilter(field_name="section", help_text="Exact section; present-but-empty selects the unnamed section.")
    category = django_filters.UUIDFilter(field_name="category__uid", help_text="Category uid.")
    updated_after = django_filters.IsoDateTimeFilter(field_name="updated_at", lookup_expr="gte")
    include_archived = django_filters.BooleanFilter(method="keep", help_text="List archived FAQs too.")
    verified = django_filters.BooleanFilter(field_name="verified_at", lookup_expr="isnull", exclude=True, help_text="true: reviewed since the last content change.")

    class Meta:
        model = Faq
        fields: list[str] = []

    def keep(self, queryset, name, value):
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if "section" in self.data and self.data.get("section") == "":
            queryset = queryset.filter(section="")
        return queryset


class FaqCategoryFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = FaqCategory
        fields: list[str] = []
