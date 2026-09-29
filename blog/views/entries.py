"""``content/entries/`` CRUD + ``check-slug/`` (module ``blogs``); workflow actions in ``entry_actions``."""

from __future__ import annotations

import uuid

from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from blog.filters import EntryFilter
from blog.models import Collection, Entry
from blog.serializers.entries import CheckSlugQuerySerializer, CheckSlugSerializer, EntryDetailSerializer, EntryListSerializer, EntryUpdateSerializer, EntryWriteSerializer
from blog.services import entries, slugs
from blog.views.entry_actions import ACTION_PERMISSIONS, ERRORS, TAGS, EntryActionsMixin
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin


@extend_schema_view(
    list=extend_schema(operation_id="content_entries_list", tags=TAGS),
    retrieve=extend_schema(operation_id="content_entries_retrieve", responses={200: EntryDetailSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="content_entries_create", request=EntryWriteSerializer, responses={201: EntryDetailSerializer, **ERRORS}, tags=TAGS),
    partial_update=extend_schema(
        operation_id="content_entries_update",
        request=EntryUpdateSerializer,
        responses={200: EntryDetailSerializer, **ERRORS},
        tags=TAGS,
        description="Child collections are replace-all: sending content_blocks / images / attribute_values / *_uids replaces the set; omitting a key keeps it.",
    ),
    destroy=extend_schema(operation_id="content_entries_delete", responses={204: OpenApiResponse(description="Deleted (soft); the slug is freed."), **ERRORS}, tags=TAGS),
)
class EntryViewSet(EntryActionsMixin, ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "blogs"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "check_slug": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        **ACTION_PERMISSIONS,
    }
    services = {"create": entries.create_entry, "update": entries.update_entry, "destroy": entries.delete_entry}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = EntryListSerializer
    filterset_class = EntryFilter
    search_fields = ["title", "slug"]
    ordering_fields = ["updated_at", "created_at", "published_on", "title", "sort_order"]
    ordering = ["-updated_at"]

    def base_queryset(self):
        return entries.list_queryset() if self.action == "list" else entries.detail_queryset()

    def filter_queryset(self, queryset):
        # List filters (and the default exclusion of ARCHIVED rows) never hide a record addressed by uid.
        return super().filter_queryset(queryset) if self.action == "list" else queryset

    def get_serializer_class(self):
        return {"create": EntryWriteSerializer, "partial_update": EntryUpdateSerializer, "retrieve": EntryDetailSerializer}.get(self.action, EntryListSerializer)

    def fresh_detail(self, entry: Entry) -> dict:
        return EntryDetailSerializer(entries.detail_queryset().get(pk=entry.pk), context=self.get_serializer_context()).data

    def perform_create(self, serializer):
        super().perform_create(serializer)
        serializer.instance = entries.detail_queryset().get(pk=serializer.instance.pk)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        serializer.instance = entries.detail_queryset().get(pk=serializer.instance.pk)

    @extend_schema(
        operation_id="content_entries_check_slug",
        parameters=[
            OpenApiParameter("collection", str, required=True, description="collection uid or api_uid"),
            OpenApiParameter("slug", str, required=True, description="candidate slug or raw title"),
            OpenApiParameter("exclude", str, required=False, description="uid of the entry being edited"),
        ],
        responses={200: CheckSlugSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
        tags=TAGS,
    )
    @action(detail=False, methods=["get"], url_path="check-slug", url_name="check-slug", filter_backends=[], pagination_class=None)
    def check_slug(self, request, *args, **kwargs):
        query = CheckSlugQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        collection = get_object_or_404(Collection.objects.all(), _collection_lookup(data["collection"]))
        exclude = Entry.objects.filter(uid=data["exclude"]).first() if data.get("exclude") else None
        return Response(CheckSlugSerializer(slugs.check_slug(collection, data["slug"], exclude_entry=exclude)).data)


def _collection_lookup(value: str) -> Q:
    try:
        return Q(uid=uuid.UUID(value))
    except ValueError:
        return Q(api_uid=value)
