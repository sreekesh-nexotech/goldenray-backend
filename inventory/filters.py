"""Inventory list filters (``?field=`` or ``?filter[field]=``)."""

from __future__ import annotations

import django_filters

from inventory.models import Balance, Direction, Location, Movement, Reason


class LocationFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(method="filter_office", help_text="HR office uid.")
    has_office = django_filters.BooleanFilter(method="filter_has_office", help_text="Tied to a (live) HR office.")

    class Meta:
        model = Location
        fields: list[str] = []

    def filter_office(self, queryset, name, value):
        return queryset.filter(office__uid=value, office__deleted_at__isnull=True)

    def filter_has_office(self, queryset, name, value):
        live = {"office__isnull": False, "office__deleted_at__isnull": True}
        return queryset.filter(**live) if value else queryset.exclude(**live)


class MovementFilter(django_filters.FilterSet):
    component = django_filters.UUIDFilter(field_name="component__uid", help_text="Component uid.")
    location = django_filters.UUIDFilter(field_name="location__uid", help_text="Location uid.")
    direction = django_filters.ChoiceFilter(choices=Direction.choices)
    reason = django_filters.MultipleChoiceFilter(choices=Reason.choices)
    ref_type = django_filters.CharFilter(field_name="ref_type")
    ref_uid = django_filters.UUIDFilter(field_name="ref_uid")
    date_from = django_filters.DateFilter(field_name="at", lookup_expr="date__gte", help_text="Moved on or after this day.")
    date_to = django_filters.DateFilter(field_name="at", lookup_expr="date__lte", help_text="Moved on or before this day.")

    class Meta:
        model = Movement
        fields: list[str] = []


class BalanceFilter(django_filters.FilterSet):
    STATES = (("in_stock", "In stock (> 0)"), ("zero", "Zero"), ("negative", "Negative (< 0)"))

    component = django_filters.UUIDFilter(field_name="component__uid", help_text="Component uid.")
    location = django_filters.UUIDFilter(field_name="location__uid", help_text="Location uid.")
    office = django_filters.UUIDFilter(method="filter_office", help_text="HR office uid of the location.")
    category = django_filters.CharFilter(field_name="component__category__slug", help_text="Catalog category slug.")
    state = django_filters.ChoiceFilter(choices=STATES, method="filter_state", help_text="in_stock, zero or negative.")

    class Meta:
        model = Balance
        fields: list[str] = []

    def filter_office(self, queryset, name, value):
        return queryset.filter(location__office__uid=value, location__office__deleted_at__isnull=True)

    def filter_state(self, queryset, name, value):
        return {"in_stock": queryset.filter(qty__gt=0), "zero": queryset.filter(qty=0), "negative": queryset.filter(qty__lt=0)}[value]
