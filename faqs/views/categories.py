"""``faq-categories/`` — optional FAQ grouping (module ``faqs``: view / create / edit / archive).

DELETE is a soft delete, refused with 409 ``category_in_use`` while live FAQs use the category.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from faqs.filters import FaqCategoryFilter
from faqs.serializers import FaqCategoryCreateSerializer, FaqCategorySerializer, FaqCategoryUpdateSerializer
from faqs.services import categories
from faqs.views.faqs import ERRORS, READ_ERRORS, UUID_REGEX


@extend_schema_view(
    list=extend_schema(responses={200: FaqCategorySerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer}),
    retrieve=extend_schema(responses={200: FaqCategorySerializer, **READ_ERRORS}),
    create=extend_schema(request=FaqCategoryCreateSerializer, responses={201: FaqCategorySerializer, **ERRORS}),
    partial_update=extend_schema(request=FaqCategoryUpdateSerializer, responses={200: FaqCategorySerializer, **ERRORS}),
    destroy=extend_schema(responses={204: OpenApiResponse(description="Deleted."), **ERRORS}, description="409 `category_in_use` while live FAQs use it."),
)
class FaqCategoryViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "faqs"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "partial_update": "edit", "destroy": "archive"}
    services = {"create": categories.create_category, "update": categories.update_category, "destroy": categories.delete_category}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    filterset_class = FaqCategoryFilter
    search_fields = ["name", "slug"]
    ordering_fields = ["sort_order", "name", "created_at"]

    def base_queryset(self):
        return categories.categories_queryset()

    def get_serializer_class(self):
        return {"create": FaqCategoryCreateSerializer, "partial_update": FaqCategoryUpdateSerializer}.get(self.action, FaqCategorySerializer)
