"""``inventory/locations/`` — CRUD (module ``inventory``: view / edit; flag ``INVENTORY_STOCK``, 404 while off).

Delete is soft and refused with 409 ``location_has_stock`` while any component has a non-zero balance there.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from core.flags import FlagRequiredMixin
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from inventory.filters import LocationFilter
from inventory.serializers.locations import LocationCreateSerializer, LocationSerializer, LocationUpdateSerializer
from inventory.services import locations
from inventory.services.common import FLAG

TAGS = ["inventory"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, **READ_ERRORS, 409: ErrorSerializer}
FLAG_NOTE = "Answers 404 while the INVENTORY_STOCK flag is off."


@extend_schema_view(
    list=extend_schema(operation_id="inventory_locations_list", tags=TAGS, description=FLAG_NOTE),
    retrieve=extend_schema(operation_id="inventory_locations_retrieve", responses={200: LocationSerializer, **READ_ERRORS}, tags=TAGS, description=FLAG_NOTE),
    create=extend_schema(operation_id="inventory_locations_create", request=LocationCreateSerializer, responses={201: LocationSerializer, **WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="inventory_locations_update", request=LocationUpdateSerializer, responses={200: LocationSerializer, **WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="inventory_locations_delete",
        responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS},
        tags=TAGS,
        description="Soft delete; 409 `location_has_stock` while any component has a non-zero balance at the location.",
    ),
)
class LocationViewSet(FlagRequiredMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    required_flag = FLAG
    module = "inventory"
    action_permissions = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}
    services = {"create": locations.create_location, "update": locations.update_location, "destroy": locations.delete_location}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = LocationSerializer
    write_serializers = {"create": LocationCreateSerializer, "partial_update": LocationUpdateSerializer}
    filterset_class = LocationFilter
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name", "created_at"]
    ordering = ["code"]

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)

    def base_queryset(self):
        return locations.locations_queryset()
