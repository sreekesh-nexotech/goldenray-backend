"""List filters for ``customers/`` (``?field=`` or ``?filter[field]=``)."""

import django_filters

from customers.models import Customer


class CustomerFilter(django_filters.FilterSet):
    source = django_filters.ChoiceFilter(choices=Customer.Source.choices)
    owner = django_filters.UUIDFilter(field_name="owner__uid", help_text="Owner (user) uid.")
    unowned = django_filters.BooleanFilter(field_name="owner", lookup_expr="isnull", help_text="true: customers without an owner.")
    pincode = django_filters.CharFilter(field_name="pincode")
    district = django_filters.CharFilter(field_name="district", lookup_expr="iexact")
    phone = django_filters.CharFilter(field_name="phone_e164", help_text="Exact E.164 number.")
    created_from = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Customer
        fields: list[str] = []
