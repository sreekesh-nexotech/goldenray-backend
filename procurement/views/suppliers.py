"""``procurement/suppliers/`` — CRUD (module ``procurement``: view · create · edit; DELETE needs edit and is refused
with 409 ``supplier_in_use`` while batches name the supplier)."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.views.common import READ_ERRORS, UUID_REGEX, WRITE_ERRORS
from procurement.models import Supplier
from procurement.serializers.shapes import SupplierSerializer, SupplierUpdateSerializer, SupplierWriteSerializer
from procurement.services import suppliers

TAGS = ["procurement"]


class SupplierFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Supplier
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="procurement_suppliers_list", tags=TAGS),
    retrieve=extend_schema(operation_id="procurement_suppliers_retrieve", responses={200: SupplierSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="procurement_suppliers_create", request=SupplierWriteSerializer, responses={201: SupplierSerializer, **WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="procurement_suppliers_update", request=SupplierUpdateSerializer, responses={200: SupplierSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="procurement_suppliers_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS}, tags=TAGS),
)
class SupplierViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "procurement"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "edit"}
    services = {"create": suppliers.create_supplier, "update": suppliers.update_supplier, "destroy": suppliers.delete_supplier}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = SupplierSerializer
    filterset_class = SupplierFilter
    search_fields = ["code", "name", "gstin"]
    ordering_fields = ["code", "name", "created_at"]
    ordering = ["code"]

    def base_queryset(self):
        return suppliers.suppliers_queryset()

    def get_serializer_class(self):
        return {"create": SupplierWriteSerializer, "partial_update": SupplierUpdateSerializer}.get(self.action, SupplierSerializer)
