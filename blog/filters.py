"""Filters of the staff entry list (``?collection=&status=&category=&tag=&template=&author=&include_archived=``).

Archived entries are held back by default so the Entries screen shows the working set (legacy behaviour); they come
back with ``status=ARCHIVED`` or ``include_archived=true``. ``collection`` accepts a uid or the public ``api_uid``.
"""

from __future__ import annotations

import uuid

import django_filters
from django.db.models import Q

from blog.models import Entry


def _uid_or(value: str, field: str) -> Q:
    try:
        return Q(**{f"{field}__uid": uuid.UUID(str(value))})
    except ValueError:
        return Q(pk__in=[])


class EntryFilter(django_filters.FilterSet):
    collection = django_filters.CharFilter(method="filter_collection", help_text="collection uid or api_uid")
    status = django_filters.MultipleChoiceFilter(choices=Entry.Status.choices)
    category = django_filters.UUIDFilter(field_name="categories__uid", help_text="category uid")
    tag = django_filters.UUIDFilter(field_name="tags__uid", help_text="tag uid")
    template = django_filters.UUIDFilter(field_name="template__uid", help_text="template uid")
    author = django_filters.UUIDFilter(field_name="author__uid", help_text="author uid")
    is_featured = django_filters.BooleanFilter()
    scheduled = django_filters.BooleanFilter(field_name="scheduled_for", lookup_expr="isnull", exclude=True, help_text="only entries waiting for a scheduled publication")
    include_archived = django_filters.BooleanFilter(method="filter_noop", help_text="include ARCHIVED entries without filtering on status")

    class Meta:
        model = Entry
        fields = ["collection", "status", "category", "tag", "template", "author", "is_featured", "scheduled", "include_archived"]

    def filter_collection(self, queryset, name, value):
        return queryset.filter(_uid_or(value, "collection") | Q(collection__api_uid=value))

    def filter_noop(self, queryset, name, value):
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        statuses = self.form.cleaned_data.get("status") or []
        if not self.form.cleaned_data.get("include_archived") and Entry.Status.ARCHIVED not in statuses:
            queryset = queryset.exclude(status=Entry.Status.ARCHIVED)
        return queryset.distinct() if self.form.cleaned_data.get("category") or self.form.cleaned_data.get("tag") else queryset
