"""``media/`` — the media library (module ``media``; PLAN §3.4 Media).

``media.view`` list/detail/signed-url · ``media.create`` upload · ``media.edit`` alt text/caption · ``media.archive``
delete (409 ``media_in_use`` while referenced). Assets in reserved folders (documents, resumes, …) are invisible
here; their owning contexts serve them. Upload validation, storage and usage checks live in ``media.services``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from media.filters import MediaAssetFilter
from media.serializers import MediaAssetSerializer, MediaAssetUpdateSerializer, MediaUploadSerializer, SignedUrlSerializer
from media.services import assets, signing

TAGS = ["media"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="media_list", tags=TAGS),
    retrieve=extend_schema(operation_id="media_retrieve", responses={200: MediaAssetSerializer, 404: ErrorSerializer}, tags=TAGS),
    partial_update=extend_schema(operation_id="media_update", request=MediaAssetUpdateSerializer, responses={200: MediaAssetSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="media_delete",
        responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS},
        tags=TAGS,
        description="Refused with 409 `media_in_use` (listing the referencing records) while any live record uses the file.",
    ),
)
class MediaAssetViewSet(ListModelMixin, RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "media"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "signed_url": "view",
        "upload": "create",
        "partial_update": "edit",
        "destroy": "archive",
    }
    services = {"update": assets.update_asset, "destroy": assets.delete_asset}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = MediaAssetSerializer
    filterset_class = MediaAssetFilter
    search_fields = ["original_filename", "alternative_text", "caption"]
    ordering_fields = ["created_at", "size_bytes", "original_filename"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return assets.library_queryset()

    def get_serializer_class(self):
        return {"partial_update": MediaAssetUpdateSerializer, "upload": MediaUploadSerializer}.get(self.action, MediaAssetSerializer)

    @extend_schema(
        operation_id="media_upload",
        request={"multipart/form-data": MediaUploadSerializer},
        responses={201: MediaAssetSerializer, 413: ErrorSerializer, 503: ErrorSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description=(
            "Multipart upload. The type is detected from the content. Limits: IMAGE/PHOTO jpeg, png, webp, heic ≤ 15 MB; "
            "DOCUMENT pdf ≤ 20 MB; RESUME pdf, doc, docx ≤ 10 MB (private only); SIGNATURE png ≤ 2 MB (private only)."
        ),
    )
    @action(detail=False, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload(self, request, *args, **kwargs):
        serializer = MediaUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        asset = assets.upload(user=request.user, **serializer.validated_data)
        return Response(MediaAssetSerializer(asset, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="media_signed_url",
        responses={200: SignedUrlSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer},
        tags=TAGS,
        description="Private files: a download URL valid for 10 minutes. Public files: their CDN URL.",
    )
    @action(detail=True, methods=["get"], url_path="signed-url")
    def signed_url(self, request, *args, **kwargs):
        asset = self.get_object()
        signed = signing.signed_url(asset, version=request.version, absolute=request.build_absolute_uri)
        return Response(SignedUrlSerializer(signed).data)
