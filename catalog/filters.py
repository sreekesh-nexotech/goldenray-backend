"""Catalog list filters (``?field=`` or ``?filter[field]=``). ``search`` is full-text over the component's generated
``search`` vector (sku, name, model, brand, description; every word is a prefix match)."""

from __future__ import annotations

import re

import django_filters
from django.contrib.postgres.search import SearchQuery
from django.db.models import F
from rest_framework.filters import OrderingFilter

from catalog.models import (
    BomRole,
    Brand,
    Category,
    Component,
    ComponentPublicProfile,
    ComponentStatus,
    InverterTopology,
    InverterType,
    OverallRating,
    PanelTechnology,
    PanelType,
    ProfileStatus,
    RatingTier,
    Tier,
)

_WORD = re.compile(r"[0-9A-Za-z]+")
MAX_TERMS = 8
MAX_COMPARED = 50


def search_query(value: str) -> SearchQuery | None:
    """``'waaree 540'`` → ``waaree:* & 540:*`` (simple config). ``None`` when nothing searchable remains."""
    words = [word.lower() for word in _WORD.findall(value or "")][:MAX_TERMS]
    if not words:
        return None
    return SearchQuery(" & ".join(f"{word}:*" for word in words), search_type="raw", config="simple")


def _csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


class ComponentFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search", help_text="Full-text: sku, name, model, brand, description (prefix match per word).")
    category = django_filters.CharFilter(method="filter_category", help_text="Category slug(s), comma-separated.")
    category_uid = django_filters.UUIDFilter(field_name="category__uid")
    bom_role = django_filters.ChoiceFilter(field_name="category__bom_role", choices=BomRole.choices)
    brand = django_filters.CharFilter(method="filter_brand", help_text="Brand slug(s), comma-separated.")
    brand_uid = django_filters.UUIDFilter(field_name="brand__uid")
    status = django_filters.MultipleChoiceFilter(choices=ComponentStatus.choices)
    tier = django_filters.ChoiceFilter(method="filter_tier", choices=Tier.choices)
    is_public = django_filters.BooleanFilter()
    is_premium = django_filters.BooleanFilter()
    sku = django_filters.CharFilter(field_name="sku", lookup_expr="iexact")

    class Meta:
        model = Component
        fields: list[str] = []

    def filter_search(self, queryset, name, value):
        query = search_query(value)
        return queryset.filter(search=query) if query is not None else queryset

    def filter_category(self, queryset, name, value):
        return queryset.filter(category__slug__in=_csv(value))

    def filter_brand(self, queryset, name, value):
        return queryset.filter(brand__slug__in=_csv(value))

    def filter_tier(self, queryset, name, value):
        return queryset.filter(tiers__tier=value, tiers__deleted_at__isnull=True).distinct()


class BrandFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Brand
        fields: list[str] = []


class CategoryFilter(django_filters.FilterSet):
    bom_role = django_filters.ChoiceFilter(choices=BomRole.choices)
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Category
        fields: list[str] = []


class PublicProfileFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ProfileStatus.choices)
    category = django_filters.CharFilter(field_name="component__category__slug")
    component_uid = django_filters.UUIDFilter(field_name="component__uid")

    class Meta:
        model = ComponentPublicProfile
        fields: list[str] = []


class PublicOrderingFilter(OrderingFilter):
    """``?ordering=`` for the website lists.

    * the view's ``ordering_fields`` are public names; ``ordering_aliases`` maps them to ORM paths (``efficiency`` →
      ``component__panel_spec__efficiency_pct``), so no internal path is accepted from the query string;
    * products without a value sort last in both directions (Postgres puts NULLs first in a DESC sort);
    * ``slug``, ``id`` break ties, so pages never repeat or skip a product.

    Without ``?ordering=`` the service order applies (Kerala score descending, unscored last).
    """

    def get_schema_operation_parameters(self, view):
        fields = list(getattr(view, "ordering_fields", None) or [])
        description = f"Sort by one or more of {', '.join(fields)} (comma-separated; prefix - for descending). Products without the value come last."
        return [{"name": self.ordering_param, "required": False, "in": "query", "description": description, "schema": {"type": "string"}}]

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        aliases = getattr(view, "ordering_aliases", {})
        expressions = []
        for term in ordering:
            path = aliases.get(term.lstrip("-"), term.lstrip("-"))
            expressions.append(F(path).desc(nulls_last=True) if term.startswith("-") else F(path).asc(nulls_last=True))
        return queryset.order_by(*expressions, "slug", "id")


class PublicProductFilter(django_filters.FilterSet):
    """Website filters shared by every product list (the legacy comparison pages filtered by rating, brand, score and
    warranty, and compared a selection of products — ``?ids=`` there, ``?slug=a,b`` here)."""

    search = django_filters.CharFilter(method="filter_search", help_text="Full-text over sku, name, model, brand.")
    slug = django_filters.CharFilter(method="filter_slug", help_text="Product slug(s), comma-separated (comparison of a selection).")
    brand = django_filters.CharFilter(method="filter_brand", help_text="Brand slug(s), comma-separated.")
    overall_rating = django_filters.MultipleChoiceFilter(choices=OverallRating.choices)
    min_kerala_score = django_filters.NumberFilter(field_name="kerala_climate_score", lookup_expr="gte")
    min_product_warranty = django_filters.NumberFilter(field_name="component__warranty_product_years", lookup_expr="gte")

    class Meta:
        model = ComponentPublicProfile
        fields: list[str] = []

    def filter_search(self, queryset, name, value):
        query = search_query(value)
        return queryset.filter(component__search=query) if query is not None else queryset

    def filter_brand(self, queryset, name, value):
        return queryset.filter(component__brand__slug__in=_csv(value))

    def filter_slug(self, queryset, name, value):
        return queryset.filter(slug__in=_csv(value)[:MAX_COMPARED])


class PublicPanelFilter(PublicProductFilter):
    panel_type = django_filters.MultipleChoiceFilter(field_name="component__panel_spec__panel_type", choices=PanelType.choices)
    technology = django_filters.MultipleChoiceFilter(field_name="component__panel_spec__technology", choices=PanelTechnology.choices)
    subsidy_eligible = django_filters.BooleanFilter()
    min_performance_warranty = django_filters.NumberFilter(field_name="component__warranty_performance_years", lookup_expr="gte")
    min_efficiency = django_filters.NumberFilter(field_name="component__panel_spec__efficiency_pct", lookup_expr="gte")
    max_efficiency = django_filters.NumberFilter(field_name="component__panel_spec__efficiency_pct", lookup_expr="lte")


class PublicInverterFilter(PublicProductFilter):
    inverter_type = django_filters.MultipleChoiceFilter(field_name="component__inverter_spec__inverter_type", choices=InverterType.choices)
    topology = django_filters.MultipleChoiceFilter(field_name="component__inverter_spec__topology", choices=InverterTopology.choices)
    rating_tier = django_filters.MultipleChoiceFilter(choices=RatingTier.choices)
    min_extendable_warranty = django_filters.NumberFilter(field_name="component__warranty_extendable_years", lookup_expr="gte")


class PublicBatteryFilter(PublicProductFilter):
    min_capacity_kwh = django_filters.NumberFilter(field_name="component__battery_spec__capacity_kwh", lookup_expr="gte")
