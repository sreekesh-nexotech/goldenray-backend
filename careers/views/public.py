"""Website careers endpoints (``/api/public/v1/``): ``job-positions/``, ``job-positions/<slug>/``, ``job-applications/``.

GETs are anonymous, throttled ``public_read`` and served from the version-keyed cache (namespaces
``careers:positions`` + ``company``). The application POST is multipart, throttled ``public_write``, honours
``Idempotency-Key`` (a retried submission never creates a second application) and never caches.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from careers.serializers.public import JobApplicationReceiptSerializer, PublicJobApplicationSerializer, PublicJobDetailSerializer, PublicJobListSerializer
from careers.services import applications, public
from core.idempotency import idempotent
from core.serializers import ErrorSerializer
from core.views import PublicAPIView
from flarize.cache_utils import cache_response
from flarize.client_ip import get_client_ip

TAGS = ["public"]
RECEIVED_MESSAGE = "Application received. Our team will get in touch if there's a fit."


class PublicJobPositionListView(PublicAPIView):
    @extend_schema(
        operation_id="public_job_positions_list",
        parameters=[OpenApiParameter("department", OpenApiTypes.STR, description="Department slug.")],
        responses={200: PublicJobListSerializer},
        tags=TAGS,
        auth=[],
        description="Published postings (the legacy CMS `/api/job-positions` payload; `uid` replaces the integer `id`).",
    )
    @cache_response(namespaces=public.CACHE_NAMESPACES, ttl=300)
    def get(self, request, *args, **kwargs):
        return Response(public.public_list(department=request.query_params.get("department") or None))


class PublicJobPositionDetailView(PublicAPIView):
    @extend_schema(
        operation_id="public_job_positions_retrieve",
        responses={200: PublicJobDetailSerializer, 404: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="A published or closed posting with its JobPosting JSON-LD; drafts and archived postings are 404.",
    )
    @cache_response(namespaces=public.CACHE_NAMESPACES, ttl=300)
    def get(self, request, slug: str, *args, **kwargs):
        return Response(public.public_detail(slug))


class PublicJobApplicationView(PublicAPIView):
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        operation_id="public_job_applications_create",
        request={"multipart/form-data": PublicJobApplicationSerializer},
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, description="8–128 characters; a retry replays the first response.")],
        responses={
            201: JobApplicationReceiptSerializer,
            400: ErrorSerializer,
            409: OpenApiResponse(response=ErrorSerializer, description="`idempotency_in_progress`"),
            413: ErrorSerializer,
            422: OpenApiResponse(response=ErrorSerializer, description="`idempotency_key_reused`"),
            429: ErrorSerializer,
            503: ErrorSerializer,
        },
        tags=TAGS,
        auth=[],
        description="The careers form (legacy field names and rules). Resume/portfolio: PDF, DOC or DOCX ≤ 10 MB, stored privately.",
    )
    @idempotent("careers.job_application")
    def post(self, request, *args, **kwargs):
        serializer = PublicJobApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data, resume, portfolio = serializer.to_service()
        application = applications.submit_application(data=data, resume=resume, portfolio=portfolio, ip=get_client_ip(request))
        receipt = {"uid": application.uid, "display_position": application.display_position, "created_at": application.created_at, "message": RECEIVED_MESSAGE}
        return Response(JobApplicationReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED)
