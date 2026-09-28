"""``documents/`` — render job status, single-use download links and the download itself (PLAN §2.1).

A job inherits the permission of the record it was rendered for (``documents.access``): the caller needs the kind's
registry permission (403 otherwise) and must be able to see the record (404 otherwise). The download URL is the
capability for ``documents/download/<token>/`` (a browser navigation cannot carry the staff JWT): single use, 10
minutes, 410 once used or expired.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.serializers import ErrorSerializer
from documents import access
from documents.serializers import DownloadLinkSerializer, RenderJobSerializer
from documents.services import downloads, jobs
from flarize.client_ip import get_client_ip
from media.services import delivery

TAGS = ["documents"]
_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}


class RenderJobDetailView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "staff"

    @extend_schema(operation_id="documents_job_retrieve", responses={200: RenderJobSerializer, **_ERRORS}, tags=TAGS)
    def get(self, request, uid, *args, **kwargs):
        job = jobs.get_job(uid)
        access.ensure_can_view(request.user, job)
        return Response(RenderJobSerializer(job).data)


class RenderJobDownloadUrlView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "staff"

    @extend_schema(
        operation_id="documents_job_download_url",
        request=None,
        responses={201: DownloadLinkSerializer, 409: ErrorSerializer, **_ERRORS},
        tags=TAGS,
        description="Issues a single-use download URL valid for 10 minutes (409 `document_not_ready` until the job is DONE).",
    )
    def post(self, request, uid, *args, **kwargs):
        link = downloads.signed_url(jobs.get_job(uid), user=request.user, version=request.version, absolute=request.build_absolute_uri)
        return Response(DownloadLinkSerializer(link).data, status=status.HTTP_201_CREATED)


class DocumentDownloadView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_scope = "staff"

    @extend_schema(
        operation_id="documents_download",
        auth=[],
        responses={
            (200, "application/pdf"): OpenApiResponse(response=OpenApiTypes.BINARY, description="The PDF (or an X-Accel-Redirect to it)."),
            403: ErrorSerializer,
            404: ErrorSerializer,
            410: ErrorSerializer,
        },
        tags=TAGS,
    )
    def get(self, request, token: str, *args, **kwargs):
        job = downloads.redeem(token, ip=get_client_ip(request), user_agent=request.headers.get("User-Agent", ""))
        filename = f"{job.kind.lower()}-{job.object_uid}-{job.language}.pdf"
        return delivery.private_file_response(job.file, content_type="application/pdf", filename=filename, inline=False)
