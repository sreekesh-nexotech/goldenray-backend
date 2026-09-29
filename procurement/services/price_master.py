"""``GET procurement/price-master/`` — the current purchase price and landed cost per component, with the batch that
set them (Flarize ``procurementWorkspace.priceMasterView``: derived from the append-only price rows, never a second
table; a component without a committed version is absent — never a zero).

Paged over components (one query for the page, one for their current PURCHASE/LANDED rows, one for the batch lines
that wrote those rows), so the endpoint is N+1 free.
"""

from __future__ import annotations

import django_filters
from django.db.models import Exists, OuterRef, Q

from catalog.models import Component
from pricing.models import CurrentPrice, Price, PriceKind
from procurement.models import BatchLine

KINDS = (PriceKind.PURCHASE, PriceKind.LANDED)


def components_queryset():
    has_cost = Price.objects.filter(component=OuterRef("pk"), kind__in=KINDS, effective_to__isnull=True)
    return Component.objects.filter(Exists(has_cost)).select_related("category").order_by("sku", "id")


class PriceMasterFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method="filter_search", help_text="SKU or name contains.")
    category = django_filters.CharFilter(field_name="category__slug", help_text="Category slug.")
    supplier_uid = django_filters.UUIDFilter(method="filter_supplier", help_text="Current rows written for this supplier.")

    class Meta:
        model = Component
        fields: list[str] = []

    def filter_search(self, queryset, name, value):
        return queryset.filter(Q(sku__icontains=value) | Q(name__icontains=value))

    def filter_supplier(self, queryset, name, value):
        return queryset.filter(Exists(Price.objects.filter(component=OuterRef("pk"), kind__in=KINDS, effective_to__isnull=True, supplier__uid=value)))


def entries_for(components) -> list[dict]:
    pks = [component.pk for component in components]
    rows = {}
    for row in CurrentPrice.objects.filter(component_id__in=pks, kind__in=KINDS).select_related("supplier"):
        rows[(row.component_id, row.kind)] = row
    price_ids = [row.pk for row in rows.values()]
    lines = {}
    for line in BatchLine.all_objects.filter(Q(price_row_id__in=price_ids) | Q(landed_row_id__in=price_ids)).select_related("batch"):
        for column in ("price_row_id", "landed_row_id"):
            if getattr(line, column):
                lines[getattr(line, column)] = line
    entries = []
    for component in components:
        purchase, landed = rows.get((component.pk, PriceKind.PURCHASE)), rows.get((component.pk, PriceKind.LANDED))
        source_line = lines.get(landed.pk if landed else None) or lines.get(purchase.pk if purchase else None)
        supplier = (landed or purchase).supplier if (landed or purchase) else None
        entries.append(
            {
                "component": component,
                "purchase": purchase,
                "landed": landed,
                "supplier": supplier,
                "batch": source_line.batch if source_line else None,
                "currency": (landed or purchase).currency if (landed or purchase) else "INR",
            }
        )
    return entries
