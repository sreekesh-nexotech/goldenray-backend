"""``catalog/public-profiles/`` — website marketing data per component (module ``products_public``).

``products_public.view`` list/detail · ``products_public.edit`` create/edit/delete · ``products_public.publish``
``publish/`` and ``unpublish/``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.filters import PublicProfileFilter
from catalog.serializers.masters import VersionOnlySerializer
from catalog.serializers.profiles import PublicProfileSerializer, PublicProfileUpdateSerializer, PublicProfileWriteSerializer
from catalog.services import profiles
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["catalog"]
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="catalog_public_profiles_list", tags=TAGS),
    retrieve=extend_schema(operation_id="catalog_public_profiles_retrieve", responses={200: PublicProfileSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="catalog_public_profiles_create", request=PublicProfileWriteSerializer, responses={201: PublicProfileSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="catalog_public_profiles_update", request=PublicProfileUpdateSerializer, responses={200: PublicProfileSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="catalog_public_profiles_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **_WRITE_ERRORS}, tags=TAGS),
)
class PublicProfileViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "products_public"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "edit",
        "partial_update": "edit",
        "destroy": "edit",
        "publish": "publish",
        "unpublish": "publish",
    }
    services = {"create": profiles.create_profile, "update": profiles.update_profile, "destroy": profiles.delete_profile}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = PublicProfileSerializer
    filterset_class = PublicProfileFilter
    search_fields = ["slug", "headline", "component__sku", "component__name"]
    ordering_fields = ["slug", "kerala_climate_score", "published_at", "updated_at"]
    ordering = ["slug"]

    def base_queryset(self):
        return profiles.profiles_queryset()

    def get_serializer_class(self):
        return {"create": PublicProfileWriteSerializer, "partial_update": PublicProfileUpdateSerializer}.get(self.action, PublicProfileSerializer)

    def _transition(self, request, service):
        body = VersionOnlySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        profile = service(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"))
        return Response(PublicProfileSerializer(profile, context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="catalog_public_profiles_publish",
        request=VersionOnlySerializer,
        responses={200: PublicProfileSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Shows the product on the website. 409 `component_not_publishable` unless the component is live, `is_public` and ACTIVE or DEPRECATED.",
    )
    @action(detail=True, methods=["post"])
    def publish(self, request, *args, **kwargs):
        return self._transition(request, profiles.publish)

    @extend_schema(operation_id="catalog_public_profiles_unpublish", request=VersionOnlySerializer, responses={200: PublicProfileSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def unpublish(self, request, *args, **kwargs):
        return self._transition(request, profiles.unpublish)
