"""``careers/applications/`` (module ``applications``: view / edit / archive).

view: list, detail, ``notes/`` (GET), ``events/``, ``download/<kind>/`` · edit: ``status/``, ``assign/``, ``notes/``
(POST) · archive: ``DELETE`` (archive; the row, files, notes and timeline are kept) and ``restore/``. The queue hides
archived applications unless ``?include_archived=true``; a detail call always reaches one.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status as http
from rest_framework.decorators import action
from rest_framework.response import Response

from careers.filters import JobApplicationFilter
from careers.serializers.applications import (
    ApplicationActionSerializer,
    ApplicationAssignSerializer,
    ApplicationFileLinkSerializer,
    ApplicationNoteCreateSerializer,
    ApplicationStatusSerializer,
    JobApplicationEventSerializer,
    JobApplicationListSerializer,
    JobApplicationNoteSerializer,
    JobApplicationSerializer,
)
from careers.services import applications
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, DestroyModelMixin, ListModelMixin, RetrieveModelMixin
from flarize.pagination import CreatedAtCursorPagination

TAGS = ["careers"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
_WRITE_ERRORS = {400: ErrorSerializer, **_READ_ERRORS, 409: ErrorSerializer}
TRUE = ("1", "true", "yes")


class EventCursorPagination(CreatedAtCursorPagination):
    page_size = 50


@extend_schema_view(
    list=extend_schema(
        operation_id="careers_applications_list",
        parameters=[OpenApiParameter("include_archived", OpenApiTypes.BOOL, description="Also list archived applications.")],
        tags=TAGS,
    ),
    retrieve=extend_schema(operation_id="careers_applications_retrieve", responses={200: JobApplicationSerializer, **_READ_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="careers_applications_archive",
        responses={204: OpenApiResponse(description="Archived."), **_WRITE_ERRORS},
        tags=TAGS,
        description="Archives (never deletes) the application; `restore/` brings it back.",
    ),
)
class JobApplicationViewSet(ListModelMixin, RetrieveModelMixin, DestroyModelMixin, BaseViewSet):
    module = "applications"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "events": "view",
        "download": "view",
        "notes": "view",
        "add_note": "edit",
        "status": "edit",
        "assign": "edit",
        "destroy": "archive",
        "restore": "archive",
    }
    services = {"destroy": applications.archive_application}
    http_method_names = ["get", "post", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = JobApplicationSerializer
    filterset_class = JobApplicationFilter
    search_fields = ["name", "email", "phone_e164", "position_title", "position_label", "location"]
    ordering_fields = ["created_at", "status_changed_at", "name", "status"]
    ordering = ["-created_at"]

    def base_queryset(self):
        include_archived = self.action != "list" or self.request.query_params.get("include_archived", "").lower() in TRUE
        return applications.applications_queryset(include_archived=include_archived)

    def get_serializer_class(self):
        return JobApplicationListSerializer if self.action == "list" else JobApplicationSerializer

    def _detail(self, application):
        return Response(JobApplicationSerializer(applications.applications_queryset(include_archived=True).get(pk=application.pk)).data)

    @extend_schema(operation_id="careers_applications_status", request=ApplicationStatusSerializer, responses={200: JobApplicationSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def status(self, request, *args, **kwargs):
        application = self.get_object()
        body = ApplicationStatusSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        updated = applications.change_status(application, user=request.user, status=data["status"], note=data.get("note", ""), expected_version=data.get("expected_version"))
        return self._detail(updated)

    @extend_schema(
        operation_id="careers_applications_assign",
        request=ApplicationAssignSerializer,
        responses={200: JobApplicationSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Link a posting (its title/department are snapshotted; the submitted position text is kept) and/or set the assignee.",
    )
    @action(detail=True, methods=["post"])
    def assign(self, request, *args, **kwargs):
        application = self.get_object()
        body = ApplicationAssignSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        updated = applications.assign(application, user=request.user, expected_version=expected, **data)
        return self._detail(updated)

    @extend_schema(operation_id="careers_applications_notes", responses={200: JobApplicationNoteSerializer(many=True), **_READ_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["get"])
    def notes(self, request, *args, **kwargs):
        application = self.get_object()
        page = self.paginate_queryset(application.notes.select_related("created_by").order_by("-created_at", "-id"))
        return self.get_paginated_response(JobApplicationNoteSerializer(page, many=True).data)

    @extend_schema(
        operation_id="careers_applications_notes_create",
        request=ApplicationNoteCreateSerializer,
        responses={201: JobApplicationNoteSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Internal note; never shown to the candidate.",
    )
    @notes.mapping.post
    def add_note(self, request, *args, **kwargs):
        application = self.get_object()
        body = ApplicationNoteCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        note = applications.add_note(application, user=request.user, body=body.validated_data["body"])
        return Response(JobApplicationNoteSerializer(note).data, status=http.HTTP_201_CREATED)

    @extend_schema(operation_id="careers_applications_events", responses={200: JobApplicationEventSerializer(many=True), **_READ_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["get"], pagination_class=EventCursorPagination)
    def events(self, request, *args, **kwargs):
        application = self.get_object()
        page = self.paginate_queryset(application.events.select_related("actor"))
        return self.get_paginated_response(JobApplicationEventSerializer(page, many=True).data)

    @extend_schema(operation_id="careers_applications_restore", request=ApplicationActionSerializer, responses={200: JobApplicationSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def restore(self, request, *args, **kwargs):
        application = self.get_object()
        body = ApplicationActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(applications.restore_application(application, user=request.user, expected_version=body.validated_data.get("expected_version")))

    @extend_schema(
        operation_id="careers_applications_download",
        parameters=[OpenApiParameter("kind", OpenApiTypes.STR, OpenApiParameter.PATH, enum=["resume", "portfolio"])],
        responses={200: ApplicationFileLinkSerializer, **_READ_ERRORS},
        tags=TAGS,
        description="A signed 10-minute URL for the private resume/portfolio (the file is named after the candidate).",
    )
    @action(detail=True, methods=["get"], url_path="download/(?P<kind>resume|portfolio)")
    def download(self, request, kind: str, *args, **kwargs):
        link = applications.download_link(self.get_object(), kind, version=request.version, absolute=request.build_absolute_uri)
        return Response(ApplicationFileLinkSerializer(link).data)
