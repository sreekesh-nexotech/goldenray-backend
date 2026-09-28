"""``reference/*`` staff CRUD (module ``reference_data``: view / create / edit / archive).

``reference/pincodes/`` (with nested post offices), ``reference/tariffs/``, ``reference/device-types/``,
``reference/wattages/``, ``reference/room-sizes/``, ``reference/ev-cars/``, ``reference/ev-scooters/``,
``reference/appliances/``. ``DELETE`` is a soft delete; ``PATCH`` accepts ``expected_version``.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from reference.models import Pincode
from reference.serializers.lists import SERIALIZERS
from reference.serializers.pincodes import PincodeCreateSerializer, PincodeSerializer, PincodeUpdateSerializer
from reference.services import lists, pincodes

TAGS = ["reference"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
_WRITE_ERRORS = {400: ErrorSerializer, **_READ_ERRORS, 409: ErrorSerializer}
ACTION_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}


class _ReferenceViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "reference_data"
    action_permissions = ACTION_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: tuple = ()

    def get_serializer_class(self):
        create, update = self.write_serializers
        return {"create": create, "partial_update": update}.get(self.action, self.serializer_class)


def _schema(name: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"reference_{name}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"reference_{name}_retrieve", responses={200: read, **_READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"reference_{name}_create", request=create, responses={201: read, **_WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"reference_{name}_update", request=update, responses={200: read, **_WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"reference_{name}_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
    )


def _filterset(model):
    return type(f"{model.__name__}Filter", (django_filters.FilterSet,), {"is_active": django_filters.BooleanFilter(), "Meta": type("Meta", (), {"model": model, "fields": []})})


def list_viewset(spec: lists.ListSpec):
    """The staff viewset of one flat list (the seven lists differ only by their ``ListSpec``)."""
    read, _public, create, update = SERIALIZERS[spec.key]
    name = spec.key.replace("-", "_")
    attrs = {
        "services": lists.services_for(spec),
        "serializer_class": read,
        "write_serializers": (create, update),
        "filterset_class": _filterset(spec.model),
        "search_fields": list(spec.search_fields),
        "ordering_fields": ["sort_order", "created_at", *spec.fields[:1]],
        "ordering": list(spec.model._meta.ordering),
        "base_queryset": lambda self, spec=spec: lists.queryset(spec),
        "__module__": __name__,
        "__doc__": f"``reference/{spec.key}/`` staff CRUD.",
    }
    return _schema(name, read, create, update)(type(f"{spec.model.__name__}ViewSet", (_ReferenceViewSet,), attrs))


LIST_VIEWSETS = {key: list_viewset(spec) for key, spec in lists.SPECS.items()}


@_schema("pincodes", PincodeSerializer, PincodeCreateSerializer, PincodeUpdateSerializer)
class PincodeViewSet(_ReferenceViewSet):
    """``reference/pincodes/`` — staff CRUD of pincodes and their post offices (the website can only look one up)."""

    services = {"create": pincodes.create_pincode, "update": pincodes.update_pincode, "destroy": pincodes.delete_pincode}
    serializer_class = PincodeSerializer
    write_serializers = (PincodeCreateSerializer, PincodeUpdateSerializer)
    search_fields = ["pincode", "district", "offices__office_name"]
    ordering_fields = ["pincode", "district", "created_at"]
    ordering = ["pincode"]

    class filterset_class(django_filters.FilterSet):  # noqa: N801 - DRF attribute name
        is_active = django_filters.BooleanFilter()
        serviceable = django_filters.BooleanFilter()
        district = django_filters.CharFilter(lookup_expr="iexact")

        class Meta:
            model = Pincode
            fields: list[str] = []

    def base_queryset(self):
        return pincodes.pincodes_queryset()
