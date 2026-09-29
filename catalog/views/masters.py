"""``catalog/brands/``, ``catalog/categories/``, ``catalog/battery-families/`` — CRUD (module ``catalog``).

``catalog.view`` list/detail · ``catalog.create`` create · ``catalog.edit`` edit · ``catalog.archive`` delete
(409 while components/specs reference the row).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from catalog.filters import BrandFilter, CategoryFilter
from catalog.serializers.masters import (
    BatteryFamilySerializer,
    BatteryFamilyUpdateSerializer,
    BatteryFamilyWriteSerializer,
    BrandSerializer,
    BrandUpdateSerializer,
    BrandWriteSerializer,
    CategorySerializer,
    CategoryUpdateSerializer,
    CategoryWriteSerializer,
)
from catalog.services import brands, categories, families
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["catalog"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
CRUD_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}


def crud_schema(prefix: str, read, create, update, delete_note: str):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, 404: ErrorSerializer}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **_WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **_WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **_WRITE_ERRORS}, tags=TAGS, description=delete_note),
    )


class _CrudViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "catalog"
    action_permissions = CRUD_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)


@crud_schema("catalog_brands", BrandSerializer, BrandWriteSerializer, BrandUpdateSerializer, "Refused with 409 `brand_in_use` while live components use the brand.")
class BrandViewSet(_CrudViewSet):
    services = {"create": brands.create_brand, "update": brands.update_brand, "destroy": brands.delete_brand}
    serializer_class = BrandSerializer
    write_serializers = {"create": BrandWriteSerializer, "partial_update": BrandUpdateSerializer}
    filterset_class = BrandFilter
    search_fields = ["name", "slug", "country"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return brands.brands_queryset()


@crud_schema("catalog_categories", CategorySerializer, CategoryWriteSerializer, CategoryUpdateSerializer, "Refused with 409 `category_in_use` while live components belong to it.")
class CategoryViewSet(_CrudViewSet):
    services = {"create": categories.create_category, "update": categories.update_category, "destroy": categories.delete_category}
    serializer_class = CategorySerializer
    write_serializers = {"create": CategoryWriteSerializer, "partial_update": CategoryUpdateSerializer}
    filterset_class = CategoryFilter
    search_fields = ["slug", "name"]
    ordering_fields = ["sort_order", "name", "slug"]
    ordering = ["sort_order", "name"]

    def base_queryset(self):
        return categories.categories_queryset()


@crud_schema(
    "catalog_battery_families",
    BatteryFamilySerializer,
    BatteryFamilyWriteSerializer,
    BatteryFamilyUpdateSerializer,
    "Refused with 409 `battery_family_in_use` while batteries or inverters reference the family.",
)
class BatteryFamilyViewSet(_CrudViewSet):
    services = {"create": families.create_family, "update": families.update_family, "destroy": families.delete_family}
    serializer_class = BatteryFamilySerializer
    write_serializers = {"create": BatteryFamilyWriteSerializer, "partial_update": BatteryFamilyUpdateSerializer}
    search_fields = ["slug", "name"]
    ordering_fields = ["name", "slug"]
    ordering = ["name"]

    def base_queryset(self):
        return families.families_queryset()
