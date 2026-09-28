"""``media/download/<token>/`` — serves a private file to whoever holds a valid signed URL.

The signed URL (``media/<uid>/signed-url/``, which needs ``media.view``) is the capability: browsers follow it from
``<img>`` tags and download links, which cannot carry the staff JWT. Tokens expire after 10 minutes (410
``link_expired``); a forged or altered token is 403 ``signature_invalid``.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.serializers import ErrorSerializer
from media.services import delivery, signing


class MediaDownloadView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_scope = "staff"

    @extend_schema(
        operation_id="media_download",
        auth=[],
        responses={
            (200, "application/octet-stream"): OpenApiResponse(response=OpenApiTypes.BINARY, description="The file (or an X-Accel-Redirect to it)."),
            403: ErrorSerializer,
            404: ErrorSerializer,
            410: ErrorSerializer,
        },
        tags=["media"],
    )
    def get(self, request, token: str, *args, **kwargs):
        asset, key = signing.resolve_token(token)
        is_original = key == asset.file
        content_type = asset.mime_type if is_original else "image/webp"
        filename = asset.original_filename if is_original else f"{asset.uid}.thumb.webp"
        return delivery.private_file_response(key, content_type=content_type, filename=filename)
