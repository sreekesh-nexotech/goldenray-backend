"""``quotation-content/…`` — content versions (draft, edit, fit check, publish) and the four masters (inclusions,
tier names, testimonials, campaigns). ``quotation_content.view`` reads · ``edit`` writes · ``publish`` publishes."""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from quotations.models import Campaign, ContentStatus, ContentVersion, Inclusion, InclusionKind, SystemType, Testimonial, TierDisplayName
from quotations.serializers.content import (
    CampaignSerializer,
    ContentPublishSerializer,
    ContentVersionCreateSerializer,
    ContentVersionDetailSerializer,
    ContentVersionSerializer,
    ContentVersionUpdateSerializer,
    FitCheckSerializer,
    FitReportSerializer,
    InclusionSerializer,
    TestimonialSerializer,
    TierDisplayNameSerializer,
)
from quotations.services import content

TAGS = ["quotation-content"]
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer, 422: ErrorSerializer}


class ContentVersionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ContentStatus.choices)

    class Meta:
        model = ContentVersion
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="quotation_content_versions_list", tags=TAGS),
    retrieve=extend_schema(operation_id="quotation_content_versions_retrieve", responses={200: ContentVersionDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class ContentVersionViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "quotation_content"
    action_permissions = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "fit_check": "edit", "publish": "publish"}
    http_method_names = ["get", "post", "patch"]
    serializer_class = ContentVersionSerializer
    filterset_class = ContentVersionFilter
    ordering_fields = ["number", "created_at"]
    ordering = ["-number"]

    def base_queryset(self):
        queryset = content.versions_queryset()
        return queryset if self.action == "retrieve" else queryset.defer("language_payload", "release_payload")

    def get_serializer_class(self):
        return ContentVersionDetailSerializer if self.action == "retrieve" else ContentVersionSerializer

    def _detail(self, version, status=200):
        return Response(ContentVersionDetailSerializer(version).data, status=status)

    @extend_schema(
        operation_id="quotation_content_versions_create",
        request=ContentVersionCreateSerializer,
        responses={201: ContentVersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A new DRAFT (the given content or a copy of the published one); 409 `draft_exists`, 422 `content_invalid`.",
    )
    def create(self, request, *args, **kwargs):
        body = ContentVersionCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(content.create_draft(user=request.user, data=body.validated_data), status=201)

    @extend_schema(
        operation_id="quotation_content_versions_partial_update",
        request=ContentVersionUpdateSerializer,
        responses={200: ContentVersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Edit the DRAFT; the fit guard refuses content that does not fit its page (422 `content_invalid`).",
    )
    def partial_update(self, request, *args, **kwargs):
        body = ContentVersionUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        return self._detail(content.update_draft(self.get_object(), user=request.user, data=data, expected_version=expected))

    @extend_schema(
        operation_id="quotation_content_versions_fit_check",
        request=FitCheckSerializer,
        responses={200: FitReportSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="The bilingual fit report (engines.content_fit) of a candidate or of the stored content.",
    )
    @action(detail=True, methods=["post"], url_path="fit-check", filter_backends=[], pagination_class=None)
    def fit_check(self, request, *args, **kwargs):
        body = FitCheckSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(FitReportSerializer(content.fit_check(self.get_object(), user=request.user, language_payload=body.validated_data.get("language_payload"))).data)

    @extend_schema(
        operation_id="quotation_content_versions_publish",
        request=ContentPublishSerializer,
        responses={200: ContentVersionDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Publish the DRAFT as the ContentRelease (the masters frozen beside it); the previous one becomes SUPERSEDED.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def publish(self, request, *args, **kwargs):
        body = ContentPublishSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(content.publish(self.get_object(), user=request.user, note=body.validated_data.get("note", ""), expected_version=body.validated_data.get("expected_version")))


def _master_viewset(name: str, model, serializer, services: tuple, *, filters: dict, search: list[str], ordering: list[str], related: tuple[str, ...] = ()):
    create, update, destroy = services
    filterset = type(f"{model.__name__}Filter", (django_filters.FilterSet,), {**filters, "Meta": type("Meta", (), {"model": model, "fields": []})})

    @extend_schema_view(
        list=extend_schema(operation_id=f"quotation_content_{name}_list", tags=TAGS),
        retrieve=extend_schema(operation_id=f"quotation_content_{name}_retrieve", tags=TAGS),
        create=extend_schema(operation_id=f"quotation_content_{name}_create", responses={201: serializer, **WRITE_ERRORS}, tags=TAGS),
        partial_update=extend_schema(operation_id=f"quotation_content_{name}_partial_update", responses={200: serializer, **WRITE_ERRORS}, tags=TAGS),
        destroy=extend_schema(operation_id=f"quotation_content_{name}_destroy", responses={204: None, **WRITE_ERRORS}, tags=TAGS),
    )
    class MasterViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
        module = "quotation_content"
        action_permissions = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}
        http_method_names = ["get", "post", "patch", "delete"]
        serializer_class = serializer
        filterset_class = filterset
        search_fields = search
        ordering_fields = ordering
        services = {"create": create, "update": update, "destroy": destroy}

        def base_queryset(self):
            return model.objects.select_related(*related) if related else model.objects.all()

    MasterViewSet.__name__ = MasterViewSet.__qualname__ = f"{model.__name__}ViewSet"
    return MasterViewSet


InclusionViewSet = _master_viewset(
    "inclusions",
    Inclusion,
    InclusionSerializer,
    (content.create_inclusion, content.update_inclusion, content.delete_inclusion),
    filters={"kind": django_filters.ChoiceFilter(choices=InclusionKind.choices)},
    search=["key", "label_en"],
    ordering=["sort_order", "key"],
)
TierNameViewSet = _master_viewset(
    "tier_names",
    TierDisplayName,
    TierDisplayNameSerializer,
    (content.create_tier_name, content.update_tier_name, content.delete_tier_name),
    filters={"system_type": django_filters.ChoiceFilter(choices=SystemType.choices)},
    search=["name_en"],
    ordering=["system_type", "tier"],
)
TestimonialViewSet = _master_viewset(
    "testimonials",
    Testimonial,
    TestimonialSerializer,
    (content.create_testimonial, content.update_testimonial, content.delete_testimonial),
    filters={"is_active": django_filters.BooleanFilter(), "show_on_website": django_filters.BooleanFilter()},
    search=["customer_name", "location"],
    ordering=["sort_order", "created_at"],
    related=("photo",),
)
CampaignViewSet = _master_viewset(
    "campaigns",
    Campaign,
    CampaignSerializer,
    (content.create_campaign, content.update_campaign, content.delete_campaign),
    filters={"is_active": django_filters.BooleanFilter()},
    search=["title"],
    ordering=["starts_on", "created_at"],
    related=("image",),
)
