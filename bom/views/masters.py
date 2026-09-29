"""``bom/templates|slots|fixed-items|structure-templates|structure-items|tube-weights|package-profiles/`` CRUD —
``bom.view`` reads, ``bom.edit`` writes (PLAN §3.2, §3.4)."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from bom.models import FixedItem, PackageProfile, Slot, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from bom.serializers import masters as s
from bom.services import masters
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["bom"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
CRUD_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}


def crud_schema(prefix: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, **READ_ERRORS}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS}, tags=TAGS),
    )


class _CrudViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "bom"
    action_permissions = CRUD_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)


class TemplateFilter(django_filters.FilterSet):
    system_type = django_filters.ChoiceFilter(choices=Template._meta.get_field("system_type").choices)
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Template
        fields: list[str] = []


@crud_schema("bom_templates", s.BomTemplateSerializer, s.BomTemplateWriteSerializer, s.BomTemplateUpdateSerializer)
class TemplateViewSet(_CrudViewSet):
    services = {"create": masters.create_template, "update": masters.update_template, "destroy": masters.delete_template}
    serializer_class = s.BomTemplateSerializer
    write_serializers = {"create": s.BomTemplateWriteSerializer, "partial_update": s.BomTemplateUpdateSerializer}
    filterset_class = TemplateFilter
    search_fields = ["name", "description"]
    ordering_fields = ["system_type", "name", "created_at"]
    ordering = ["system_type"]

    def base_queryset(self):
        return Template.objects.all()


class ChildFilter(django_filters.FilterSet):
    template = django_filters.UUIDFilter(field_name="template__uid", help_text="Parent template uid.")


class SlotFilter(ChildFilter):
    category = django_filters.CharFilter(field_name="category__slug", help_text="Category slug.")

    class Meta:
        model = Slot
        fields: list[str] = []


@crud_schema("bom_slots", s.BomSlotSerializer, s.BomSlotWriteSerializer, s.BomSlotUpdateSerializer)
class SlotViewSet(_CrudViewSet):
    services = {"create": masters.create_slot, "update": masters.update_slot, "destroy": masters.delete_slot}
    serializer_class = s.BomSlotSerializer
    write_serializers = {"create": s.BomSlotWriteSerializer, "partial_update": s.BomSlotUpdateSerializer}
    filterset_class = SlotFilter
    search_fields = ["key", "label"]
    ordering_fields = ["sort_order", "key", "created_at"]
    ordering = ["template_id", "sort_order", "id"]

    def base_queryset(self):
        return Slot.objects.filter(template__deleted_at__isnull=True).select_related("template", "category")


class FixedItemFilter(ChildFilter):
    section = django_filters.CharFilter()

    class Meta:
        model = FixedItem
        fields: list[str] = []


@crud_schema("bom_fixed_items", s.BomFixedItemSerializer, s.BomFixedItemWriteSerializer, s.BomFixedItemUpdateSerializer)
class FixedItemViewSet(_CrudViewSet):
    services = {"create": masters.create_fixed_item, "update": masters.update_fixed_item, "destroy": masters.delete_fixed_item}
    serializer_class = s.BomFixedItemSerializer
    write_serializers = {"create": s.BomFixedItemWriteSerializer, "partial_update": s.BomFixedItemUpdateSerializer}
    filterset_class = FixedItemFilter
    search_fields = ["name", "code"]
    ordering_fields = ["sort_order", "name", "created_at"]
    ordering = ["template_id", "sort_order", "id"]

    def base_queryset(self):
        return FixedItem.objects.filter(template__deleted_at__isnull=True).select_related("template", "component", "category")


@crud_schema("bom_structure_templates", s.BomStructureTemplateSerializer, s.BomStructureTemplateWriteSerializer, s.BomStructureTemplateUpdateSerializer)
class StructureTemplateViewSet(_CrudViewSet):
    services = {"create": masters.create_structure_template, "update": masters.update_structure_template, "destroy": masters.delete_structure_template}
    serializer_class = s.BomStructureTemplateSerializer
    write_serializers = {"create": s.BomStructureTemplateWriteSerializer, "partial_update": s.BomStructureTemplateUpdateSerializer}
    search_fields = ["slug", "name"]
    ordering_fields = ["slug", "name", "created_at"]
    ordering = ["id"]

    def base_queryset(self):
        return StructureTemplate.objects.all()


class StructureItemFilter(ChildFilter):
    item_type = django_filters.ChoiceFilter(choices=StructureTemplateItem._meta.get_field("item_type").choices)

    class Meta:
        model = StructureTemplateItem
        fields: list[str] = []


@crud_schema("bom_structure_items", s.BomStructureItemSerializer, s.BomStructureItemWriteSerializer, s.BomStructureItemUpdateSerializer)
class StructureItemViewSet(_CrudViewSet):
    services = {"create": masters.create_structure_item, "update": masters.update_structure_item, "destroy": masters.delete_structure_item}
    serializer_class = s.BomStructureItemSerializer
    write_serializers = {"create": s.BomStructureItemWriteSerializer, "partial_update": s.BomStructureItemUpdateSerializer}
    filterset_class = StructureItemFilter
    search_fields = ["name", "tube_size"]
    ordering_fields = ["sort_order", "name", "created_at"]
    ordering = ["template_id", "sort_order", "id"]

    def base_queryset(self):
        return StructureTemplateItem.objects.filter(template__deleted_at__isnull=True).select_related("template")


@crud_schema("bom_tube_weights", s.BomTubeWeightSerializer, s.BomTubeWeightWriteSerializer, s.BomTubeWeightUpdateSerializer)
class TubeWeightViewSet(_CrudViewSet):
    services = {"create": masters.create_tube_weight, "update": masters.update_tube_weight, "destroy": masters.delete_tube_weight}
    serializer_class = s.BomTubeWeightSerializer
    write_serializers = {"create": s.BomTubeWeightWriteSerializer, "partial_update": s.BomTubeWeightUpdateSerializer}
    search_fields = ["tube_size"]
    ordering_fields = ["tube_size", "weight_kg"]
    ordering = ["tube_size"]

    def base_queryset(self):
        return TubeWeight.objects.all()


@crud_schema("bom_package_profiles", s.BomPackageProfileSerializer, s.BomPackageProfileWriteSerializer, s.BomPackageProfileUpdateSerializer)
class PackageProfileViewSet(_CrudViewSet):
    services = {"create": masters.create_package_profile, "update": masters.update_package_profile, "destroy": masters.delete_package_profile}
    serializer_class = s.BomPackageProfileSerializer
    write_serializers = {"create": s.BomPackageProfileWriteSerializer, "partial_update": s.BomPackageProfileUpdateSerializer}
    search_fields = ["key", "label"]
    ordering_fields = ["key", "label"]
    ordering = ["key"]

    def base_queryset(self):
        return PackageProfile.objects.select_related("battery_component")
