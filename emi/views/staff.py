"""``emi/*`` staff endpoints (module ``emi``: ``view`` reads, ``edit`` every write — PLAN §3.2 grants no other action).

``emi/banks/``, ``emi/interest-rules/``, ``emi/subsidy-rules/``, ``emi/system-sizes/`` (CRUD; ``DELETE`` is a soft
delete; ``PATCH``/``DELETE`` take ``expected_version``) and ``emi/settings/`` (``GET``/``PATCH`` of the singleton).
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from emi.serializers.staff import SERIALIZERS, EmiSettingsSerializer, EmiSettingsUpdateSerializer
from emi.services import rows
from emi.services.settings import current_settings, update_settings

TAGS = ["emi"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
_WRITE_ERRORS = {400: ErrorSerializer, **_READ_ERRORS, 409: ErrorSerializer}
ACTION_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}
ORDERING_FIELDS = {
    "banks": ["sort_order", "annual_rate", "name", "created_at"],
    "interest-rules": ["priority", "annual_rate", "created_at"],
    "subsidy-rules": ["priority", "kw_from", "created_at"],
    "system-sizes": ["sort_order", "capacity_kw", "created_at"],
}
SEARCH_FIELDS = {"banks": ["name", "abbr", "slug"], "interest-rules": ["label"], "subsidy-rules": ["label"], "system-sizes": ["label"]}


class _EmiViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "emi"
    action_permissions = ACTION_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: tuple = ()

    def get_serializer_class(self):
        create, update = self.write_serializers
        return {"create": create, "partial_update": update}.get(self.action, self.serializer_class)


def _schema(name: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"emi_{name}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"emi_{name}_retrieve", responses={200: read, **_READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"emi_{name}_create", request=create, responses={201: read, **_WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"emi_{name}_update", request=update, responses={200: read, **_WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"emi_{name}_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
    )


def _filterset(model):
    return type(f"Emi{model.__name__}Filter", (django_filters.FilterSet,), {"is_active": django_filters.BooleanFilter(), "Meta": type("Meta", (), {"model": model, "fields": []})})


def row_viewset(spec: rows.RowSpec):
    read, create, update = SERIALIZERS[spec.key]
    name = spec.key.replace("-", "_")
    attrs = {
        "services": rows.services_for(spec),
        "serializer_class": read,
        "write_serializers": (create, update),
        "filterset_class": _filterset(spec.model),
        "search_fields": SEARCH_FIELDS[spec.key],
        "ordering_fields": ORDERING_FIELDS[spec.key],
        "ordering": list(spec.model._meta.ordering),
        "base_queryset": lambda self, spec=spec: rows.queryset(spec),
        "__module__": __name__,
        "__doc__": f"``emi/{spec.key}/`` staff CRUD.",
    }
    return _schema(name, read, create, update)(type(f"Emi{spec.model.__name__}ViewSet", (_EmiViewSet,), attrs))


ROW_VIEWSETS = {key: row_viewset(spec) for key, spec in rows.SPECS.items()}


class EmiSettingsView(BaseAPIView):
    """``emi/settings/``: the calculator's knobs; reads serve the defaults until the first edit creates the row."""

    module = "emi"
    action_permissions = {"GET": "view", "PATCH": "edit"}

    @extend_schema(operation_id="emi_settings_retrieve", responses={200: EmiSettingsSerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        return Response(EmiSettingsSerializer(current_settings()).data)

    @extend_schema(operation_id="emi_settings_update", request=EmiSettingsUpdateSerializer, responses={200: EmiSettingsSerializer, **_WRITE_ERRORS}, tags=TAGS)
    def patch(self, request, *args, **kwargs):
        serializer = EmiSettingsUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        expected = data.pop("expected_version", None)
        return Response(EmiSettingsSerializer(update_settings(user=request.user, data=data, expected_version=expected)).data)
