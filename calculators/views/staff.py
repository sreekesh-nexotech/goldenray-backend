"""``calculators/*`` staff CRUD of the sizing tables (module ``reference_data``: view / create / edit / archive).

``calculators/capacity-sizes/`` (legacy ``solar_installations``) and ``calculators/bill-range-sizes/`` (legacy
``solar_installation_new``). ``DELETE`` is a soft delete; ``PATCH``/``DELETE`` take ``expected_version``.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from calculators.models import BillRangeSize, CapacitySize, PropertyType
from calculators.serializers.staff import SERIALIZERS
from calculators.services import sizing
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["calculators"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
_WRITE_ERRORS = {400: ErrorSerializer, **_READ_ERRORS, 409: ErrorSerializer}
ACTION_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}


class CapacitySizeFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = CapacitySize
        fields: list[str] = []


class BillRangeSizeFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    property_type = django_filters.ChoiceFilter(choices=PropertyType.choices)

    class Meta:
        model = BillRangeSize
        fields: list[str] = []


FILTERS = {sizing.CAPACITY_SIZES.key: CapacitySizeFilter, sizing.BILL_RANGE_SIZES.key: BillRangeSizeFilter}
ORDERING_FIELDS = {sizing.CAPACITY_SIZES.key: ["power_capacity_kw", "created_at"], sizing.BILL_RANGE_SIZES.key: ["bill_range", "property_type", "power_capacity_kw", "created_at"]}


class _SizingViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "reference_data"
    action_permissions = ACTION_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: tuple = ()

    def get_serializer_class(self):
        create, update = self.write_serializers
        return {"create": create, "partial_update": update}.get(self.action, self.serializer_class)


def sizing_viewset(spec: sizing.SizingSpec):
    read, create, update = SERIALIZERS[spec.key]
    name = spec.key.replace("-", "_")
    attrs = {
        "services": sizing.services_for(spec),
        "serializer_class": read,
        "write_serializers": (create, update),
        "filterset_class": FILTERS[spec.key],
        "ordering_fields": ORDERING_FIELDS[spec.key],
        "ordering": list(spec.model._meta.ordering),
        "base_queryset": lambda self, spec=spec: sizing.queryset(spec),
        "__module__": __name__,
        "__doc__": f"``calculators/{spec.key}/`` staff CRUD.",
    }
    viewset = type(f"{spec.model.__name__}ViewSet", (_SizingViewSet,), attrs)
    return extend_schema_view(
        list=extend_schema(operation_id=f"calculators_{name}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"calculators_{name}_retrieve", responses={200: read, **_READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"calculators_{name}_create", request=create, responses={201: read, **_WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"calculators_{name}_update", request=update, responses={200: read, **_WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"calculators_{name}_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
    )(viewset)


SIZING_VIEWSETS = {key: sizing_viewset(spec) for key, spec in sizing.SPECS.items()}
