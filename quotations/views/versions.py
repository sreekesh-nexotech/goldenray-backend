"""Version sub-resources of ``quotations/<uid>/versions/<n>/`` (mixed into :class:`QuotationViewSet`): detail, PATCH
of the draft, preview (payload + gate report), issue, document link, re-render, send."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from quotations.serializers.quotations import (
    DocumentLinkSerializer,
    EmailLogSerializer,
    QuotationPreviewSerializer,
    QuotationTransitionSerializer,
    RenderJobRefSerializer,
    RenderSerializer,
    SendSerializer,
    VersionDetailSerializer,
    VersionUpdateSerializer,
)
from quotations.services import lifecycle, quotations, sending

TAGS = ["quotations"]
VERSION_PATH = r"versions/(?P<number>[0-9]{1,4})"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer, 422: ErrorSerializer}
NUMBER = OpenApiParameter("number", int, OpenApiParameter.PATH, description="Version number (1..n).")
LANGUAGE = OpenApiParameter("language", str, enum=["en", "ml"], description="Document language (default: the version's).")


class VersionActionsMixin:
    def _version(self, number):
        return quotations.get_version(self.get_object(), int(number))

    def _version_response(self, version, status=200):
        version = quotations.versions_queryset().prefetch_related("bom_snapshots__engineering_run", "commercial_snapshots", "email_logs__sent_by").get(pk=version.pk)
        return Response(VersionDetailSerializer(version, context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="quotations_version_retrieve",
        parameters=[NUMBER],
        responses={200: VersionDetailSerializer, **READ_ERRORS},
        tags=TAGS,
        description="One version with its snapshots, e-mail log and frozen document (internal cost withheld without pricing_internal.view).",
    )
    @action(detail=True, methods=["get"], url_path=VERSION_PATH, filter_backends=[], pagination_class=None)
    def version_detail(self, request, number, *args, **kwargs):
        return self._version_response(self._version(number))

    @extend_schema(
        operation_id="quotations_version_partial_update",
        parameters=[NUMBER],
        request=VersionUpdateSerializer,
        responses={200: VersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Edit the DRAFT version's selections (validated against its PackRelease); `refresh_release` re-pins it to the current releases.",
    )
    @version_detail.mapping.patch
    def version_update(self, request, number, *args, **kwargs):
        version = self._version(number)
        body = VersionUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        return self._version_response(quotations.update_draft(version, user=request.user, data=data, expected_version=expected))

    @extend_schema(
        operation_id="quotations_version_preview",
        parameters=[NUMBER],
        request=None,
        responses={200: QuotationPreviewSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="The payload and the 8-check gate report of the DRAFT, priced now; nothing is written.",
    )
    @action(detail=True, methods=["post"], url_path=rf"{VERSION_PATH}/preview", filter_backends=[], pagination_class=None)
    def preview(self, request, number, *args, **kwargs):
        return Response(QuotationPreviewSerializer(quotations.preview(self._version(number), user=request.user)).data)

    @extend_schema(
        operation_id="quotations_version_issue",
        parameters=[NUMBER],
        request=QuotationTransitionSerializer,
        responses={200: VersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Issue the DRAFT: the gate must pass (422 `generation_blocked`); number, snapshots, frozen document + SHA-256, en/ml renders.",
    )
    @action(detail=True, methods=["post"], url_path=rf"{VERSION_PATH}/issue", filter_backends=[], pagination_class=None)
    def issue(self, request, number, *args, **kwargs):
        body = QuotationTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        version = quotations.issue(self._version(number), user=request.user, expected_version=body.validated_data.get("expected_version"))
        return self._version_response(version)

    @extend_schema(
        operation_id="quotations_version_document",
        parameters=[NUMBER, LANGUAGE],
        responses={200: DocumentLinkSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A single-use signed download link of the version's PDF (409 `document_not_ready` while rendering).",
    )
    @action(detail=True, methods=["get"], url_path=rf"{VERSION_PATH}/document", filter_backends=[], pagination_class=None)
    def document(self, request, number, *args, **kwargs):
        from documents.services.downloads import signed_url

        version = self._version(number)
        language = request.query_params.get("language") or version.language
        if language not in ("en", "ml"):
            language = version.language
        link = signed_url(lifecycle.document_job(version, language), user=request.user, version=request.version, absolute=request.build_absolute_uri)
        return Response(DocumentLinkSerializer({"url": link.url, "expires_at": link.expires_at, "language": language}).data)

    @extend_schema(
        operation_id="quotations_version_render",
        parameters=[NUMBER],
        request=RenderSerializer,
        responses={200: RenderJobRefSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Render the frozen document again (imported versions, failed jobs); the payload is never re-derived.",
    )
    @action(detail=True, methods=["post"], url_path=rf"{VERSION_PATH}/render", filter_backends=[], pagination_class=None)
    def render(self, request, number, *args, **kwargs):
        body = RenderSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        job = lifecycle.rerender(self._version(number), user=request.user, language=body.validated_data["language"])
        return Response(RenderJobRefSerializer({"uid": job.uid, "status": job.status, "language": job.language, "payload_sha256": job.payload_sha256}).data)

    @extend_schema(
        operation_id="quotations_version_send",
        parameters=[NUMBER],
        request=SendSerializer,
        responses={202: EmailLogSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="E-mail the rendered PDF to the customer (queued; the e-mail log records the outcome). WhatsApp: 400 `channel_unavailable`.",
    )
    @action(detail=True, methods=["post"], url_path=rf"{VERSION_PATH}/send", filter_backends=[], pagination_class=None)
    def send(self, request, number, *args, **kwargs):
        body = SendSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        log = sending.send(self._version(number), user=request.user, **body.validated_data)
        return Response(EmailLogSerializer(log).data, status=202)
