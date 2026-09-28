"""``company/profile/`` (staff, module ``company``: view / edit) and ``company/`` (public website payload)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from company.serializers.profile import CompanyProfileSerializer, CompanyProfileUpdateSerializer, PublicCompanySerializer
from company.services import profile as profiles
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, PublicAPIView
from flarize.cache_utils import cache_response

TAGS = ["company"]


class CompanyProfileView(BaseAPIView):
    module = "company"
    action_permissions = {"GET": "view", "PATCH": "edit"}

    @extend_schema(operation_id="company_profile_retrieve", responses={200: CompanyProfileSerializer, 401: ErrorSerializer, 403: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        return Response(CompanyProfileSerializer(profiles.current_profile()).data)

    @extend_schema(
        operation_id="company_profile_update",
        request=CompanyProfileUpdateSerializer,
        responses={200: CompanyProfileSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 409: ErrorSerializer},
        tags=TAGS,
    )
    def patch(self, request, *args, **kwargs):
        # Every field is optional at the top level; nested trust_stats entries stay fully validated (no partial=True).
        serializer = CompanyProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        expected = data.pop("expected_version", None)
        updated = profiles.update_profile(user=request.user, data=data, expected_version=expected)
        return Response(CompanyProfileSerializer(updated).data)


class PublicCompanyView(PublicAPIView):
    @extend_schema(operation_id="public_company_retrieve", responses={200: PublicCompanySerializer}, tags=["public"], auth=[])
    @cache_response(namespaces=[profiles.CACHE_NAMESPACE, "media"], ttl=300)
    def get(self, request, *args, **kwargs):
        return Response(PublicCompanySerializer(profiles.current_profile()).data)
