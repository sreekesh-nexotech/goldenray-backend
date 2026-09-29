"""``content/collections/``, ``content/templates/`` (+ ``duplicate/``) and the templates' nested ``image-groups/`` and
``attribute-slots/`` (module ``blogs``: view / create / edit / archive)."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from blog.models import Template, TemplateAttributeSlot, TemplateImageGroup
from blog.serializers.schema import (
    AttributeSlotUpdateSerializer,
    AttributeSlotWriteSerializer,
    CollectionSerializer,
    CollectionUpdateSerializer,
    CollectionWriteSerializer,
    ImageGroupUpdateSerializer,
    ImageGroupWriteSerializer,
    TemplateAttributeSlotSerializer,
    TemplateDuplicateSerializer,
    TemplateImageGroupSerializer,
    TemplateSerializer,
    TemplateUpdateSerializer,
    TemplateWriteSerializer,
)
from blog.services import schema
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["content"]
UID_RE = "[0-9a-fA-F-]{36}"
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
CRUD_PERMISSIONS = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}


def crud_schema(prefix: str, read, create, update):
    return extend_schema_view(
        list=extend_schema(operation_id=f"{prefix}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"{prefix}_retrieve", responses={200: read, 404: ErrorSerializer}, tags=TAGS),
        create=extend_schema(operation_id=f"{prefix}_create", request=create, responses={201: read, **ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"{prefix}_update", request=update, responses={200: read, **ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"{prefix}_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **ERRORS}, tags=TAGS),
    )


class _CrudViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "blogs"
    action_permissions = CRUD_PERMISSIONS
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UID_RE
    write_serializers: dict = {}

    def get_serializer_class(self):
        return self.write_serializers.get(self.action, self.serializer_class)

    def _reload(self, serializer):
        # Respond with the list/detail shape (annotations and prefetches included).
        serializer.instance = self.base_queryset().get(pk=serializer.instance.pk)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        self._reload(serializer)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        self._reload(serializer)


@crud_schema("content_collections", CollectionSerializer, CollectionWriteSerializer, CollectionUpdateSerializer)
class CollectionViewSet(_CrudViewSet):
    serializer_class = CollectionSerializer
    write_serializers = {"create": CollectionWriteSerializer, "partial_update": CollectionUpdateSerializer}
    services = {"create": schema.create_collection, "update": schema.update_collection, "destroy": schema.delete_collection}
    search_fields = ["api_uid", "plural_name", "singular_name"]
    ordering_fields = ["plural_name", "api_uid", "created_at"]
    ordering = ["plural_name"]
    filterset_fields = ["is_active"]

    def base_queryset(self):
        return schema.collections_queryset()


@crud_schema("content_templates", TemplateSerializer, TemplateWriteSerializer, TemplateUpdateSerializer)
class TemplateViewSet(_CrudViewSet):
    serializer_class = TemplateSerializer
    write_serializers = {"create": TemplateWriteSerializer, "partial_update": TemplateUpdateSerializer}
    action_permissions = {**CRUD_PERMISSIONS, "duplicate": "create"}
    services = {"create": schema.create_template, "update": schema.update_template, "destroy": schema.delete_template}
    search_fields = ["slug", "name"]
    ordering_fields = ["sort_order", "name", "created_at"]
    ordering = ["sort_order", "name"]
    filterset_fields = ["is_active"]

    def base_queryset(self):
        return schema.templates_queryset()

    @extend_schema(operation_id="content_templates_duplicate", request=TemplateDuplicateSerializer, responses={201: TemplateSerializer, **ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def duplicate(self, request, *args, **kwargs):
        source = self.get_object()
        serializer = TemplateDuplicateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        copy = schema.duplicate_template(source, user=request.user, **serializer.validated_data)
        return Response(TemplateSerializer(schema.templates_queryset().get(pk=copy.pk)).data, status=status.HTTP_201_CREATED)


class _TemplateChildViewSet(_CrudViewSet):
    """Children of ``content/templates/<template_uid>/``; the parent must be a live template (404 otherwise)."""

    lookup_url_kwarg = "uid"
    create_service = None
    ordering = ["position"]
    ordering_fields = ["position", "key"]

    def template(self) -> Template | None:
        if getattr(self, "swagger_fake_view", False):  # schema generation has no URL kwargs
            return None
        if not hasattr(self, "_template"):
            self._template = get_object_or_404(Template.objects.all(), uid=self.kwargs["template_uid"])
        return self._template

    def perform_create(self, serializer):
        serializer.instance = type(self).create_service(self.template(), user=self.request.user, data=dict(serializer.validated_data))
        self._reload(serializer)


@crud_schema("content_template_image_groups", TemplateImageGroupSerializer, ImageGroupWriteSerializer, ImageGroupUpdateSerializer)
class TemplateImageGroupViewSet(_TemplateChildViewSet):
    serializer_class = TemplateImageGroupSerializer
    write_serializers = {"create": ImageGroupWriteSerializer, "partial_update": ImageGroupUpdateSerializer}
    create_service = schema.create_image_group
    services = {"update": schema.update_image_group, "destroy": schema.delete_image_group}

    def base_queryset(self):
        return TemplateImageGroup.objects.filter(template=self.template())


@crud_schema("content_template_attribute_slots", TemplateAttributeSlotSerializer, AttributeSlotWriteSerializer, AttributeSlotUpdateSerializer)
class TemplateAttributeSlotViewSet(_TemplateChildViewSet):
    serializer_class = TemplateAttributeSlotSerializer
    write_serializers = {"create": AttributeSlotWriteSerializer, "partial_update": AttributeSlotUpdateSerializer}
    create_service = schema.create_attribute_slot
    services = {"update": schema.update_attribute_slot, "destroy": schema.delete_attribute_slot}

    def base_queryset(self):
        return TemplateAttributeSlot.objects.filter(template=self.template())
