"""Workflow actions of ``content/entries/<uid>/…`` (POST sub-resources; mixed into ``EntryViewSet``).

``submit/`` (edit) · ``publish/``, ``unpublish/``, ``schedule/`` (publish) · ``archive/``, ``restore/`` (archive) ·
``verify/`` (verify) · ``duplicate/`` (create) · ``preview/`` (view) · ``slug-history/`` GET (view) / POST (edit) ·
``slug-history/<alias_uid>/deactivate/`` (edit). Every action returns the fresh full entry (or the created object).
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from blog.models import EntrySlugHistory
from blog.serializers.entries import AliasCreateSerializer, EntryActionSerializer, EntryDetailSerializer, EntryScheduleSerializer, PreviewLinkSerializer, SlugHistorySerializer
from blog.services import preview, slugs, workflow
from core.serializers import ErrorSerializer

TAGS = ["content"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
ACTION_PERMISSIONS = {
    "submit": "edit",
    "publish": "publish",
    "unpublish": "publish",
    "schedule": "publish",
    "archive": "archive",
    "restore": "archive",
    "verify": "verify",
    "duplicate": "create",
    "preview": "view",
    "slug_history": "view",
    "add_alias": "edit",
    "deactivate_alias": "edit",
}


def _transition(name: str, service, description: str):
    """A POST action ``<name>/`` running ``service(entry, user=, expected_version=)`` and returning the full entry."""

    def view(self, request, *args, **kwargs):
        entry = self.get_object()
        serializer = EntryActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = service(entry, user=request.user, expected_version=serializer.validated_data.get("expected_version"))
        return Response(self.fresh_detail(updated))

    # DRF maps the route to the method by __name__, so it must be set before @action runs.
    view.__name__ = name
    view.__qualname__ = f"EntryActionsMixin.{name}"
    view = action(detail=True, methods=["post"], url_path=name, url_name=name)(view)
    return extend_schema(operation_id=f"content_entries_{name}", request=EntryActionSerializer, responses={200: EntryDetailSerializer, **ERRORS}, tags=TAGS, description=description)(view)


class EntryActionsMixin:
    submit = _transition("submit", workflow.submit_entry, "DRAFT → REVIEW.")
    publish = _transition("publish", workflow.publish_entry, "DRAFT/REVIEW → PUBLISHED after publish-time validation (400 `publish_validation_failed`).")
    unpublish = _transition("unpublish", workflow.unpublish_entry, "PUBLISHED → DRAFT (dates kept).")
    archive = _transition("archive", workflow.archive_entry, "DRAFT/REVIEW/PUBLISHED → ARCHIVED (taken down, slug kept).")
    restore = _transition("restore", workflow.restore_entry, "ARCHIVED → DRAFT.")
    verify = _transition("verify", workflow.verify_entry, "Stamp verified_by/verified_at.")

    @extend_schema(
        operation_id="content_entries_schedule",
        request=EntryScheduleSerializer,
        responses={200: EntryDetailSerializer, **ERRORS},
        tags=TAGS,
        description="Publish at a future time (validated now); null cancels.",
    )
    @action(detail=True, methods=["post"])
    def schedule(self, request, *args, **kwargs):
        entry = self.get_object()
        serializer = EntryScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        updated = workflow.schedule_entry(entry, user=request.user, scheduled_for=data["scheduled_for"], expected_version=data.get("expected_version"))
        return Response(self.fresh_detail(updated))

    @extend_schema(operation_id="content_entries_duplicate", request=None, responses={201: EntryDetailSerializer, **ERRORS}, tags=TAGS, description="A new DRAFT copy with a '-copy' slug.")
    @action(detail=True, methods=["post"])
    def duplicate(self, request, *args, **kwargs):
        copy = workflow.duplicate_entry(self.get_object(), user=request.user)
        return Response(self.fresh_detail(copy), status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="content_entries_preview", request=None, responses={201: PreviewLinkSerializer, **ERRORS}, tags=TAGS, description="A signed public preview link to the entry's current state."
    )
    @action(detail=True, methods=["post"])
    def preview(self, request, *args, **kwargs):
        link = preview.issue_preview(self.get_object(), user=request.user, version=request.version)
        return Response(PreviewLinkSerializer({"token": link.token, "url": request.build_absolute_uri(link.path), "expires_at": link.expires_at}).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="content_entries_slug_history", request=None, responses={200: SlugHistorySerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS
    )
    @action(detail=True, methods=["get"], url_path="slug-history", url_name="slug-history")
    def slug_history(self, request, *args, **kwargs):
        entry = self.get_object()
        return Response(SlugHistorySerializer(slugs.aliases_queryset(entry), many=True).data)

    @extend_schema(
        operation_id="content_entries_slug_history_add",
        request=AliasCreateSerializer,
        responses={201: SlugHistorySerializer, **ERRORS},
        tags=TAGS,
        description="Record a verified historical slug (renames that predate the alias table).",
    )
    @slug_history.mapping.post
    def add_alias(self, request, *args, **kwargs):
        entry = self.get_object()
        serializer = AliasCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        alias = slugs.add_alias(entry, user=request.user, **serializer.validated_data)
        return Response(SlugHistorySerializer(alias).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="content_entries_slug_history_deactivate",
        request=EntryActionSerializer,
        responses={200: SlugHistorySerializer, **ERRORS},
        tags=TAGS,
        description="Retire an alias (kept for audit); `expected_version` is the alias row's version.",
    )
    @action(detail=True, methods=["post"], url_path=r"slug-history/(?P<alias_uid>[0-9a-fA-F-]{36})/deactivate", url_name="slug-history-deactivate")
    def deactivate_alias(self, request, *args, **kwargs):
        entry = self.get_object()
        alias = get_object_or_404(EntrySlugHistory.objects.all(), entry=entry, uid=kwargs["alias_uid"])
        serializer = EntryActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(SlugHistorySerializer(slugs.deactivate_alias(alias, user=request.user, expected_version=serializer.validated_data.get("expected_version"))).data)
