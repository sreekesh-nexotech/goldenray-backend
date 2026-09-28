"""``pages/`` — maintained website pages (module ``pages``; PLAN §3.4 "Website content").

``pages.view`` list/detail/``seo/`` (GET)/``preview/`` · ``pages.edit`` ``PATCH`` (sort order), ``text-slots/<key>/``,
``image-slots/<key>/``, ``seo/`` (PATCH) · ``pages.publish`` ``publish/``, ``unpublish/``, ``archive/``,
``restore/`` · ``pages.verify`` ``verify/``. There is no create and no delete: pages are registered routes
(``manage.py seed_pages``). The action mixins are shared with ``career-page/`` (module ``career_page``).
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from sitepages.filters import PageFilter
from sitepages.serializers import (
    ImageSlotUpdateSerializer,
    PageActionSerializer,
    PageDetailSerializer,
    PageImageSlotSerializer,
    PageListSerializer,
    PageSeoSerializer,
    PageSeoUpdateSerializer,
    PageTextSlotSerializer,
    PageUpdateSerializer,
    PreviewSerializer,
    TextSlotUpdateSerializer,
)
from sitepages.services import content, delivery, pages

UUID_REGEX = "[0-9a-fA-F-]{36}"
SLOT_KEY = r"(?P<key>[A-Za-z][A-Za-z0-9_-]*)"
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}


def _body(serializer_class, request) -> tuple[dict, int | None]:
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    return data, data.pop("expected_version", None)


class PageViewMixin:
    def detail_response(self, page) -> Response:
        return Response(PageDetailSerializer(pages.detail(page)).data)

    def workflow(self, request, service) -> Response:
        _, expected = _body(PageActionSerializer, request)
        return self.detail_response(service(self.get_object(), user=request.user, expected_version=expected))


class PageContentActionsMixin(PageViewMixin):
    """Slots, SEO and preview of one page (``…/text-slots/<key>/``, ``…/image-slots/<key>/``, ``…/seo/``, ``…/preview/``)."""

    @extend_schema(request=TextSlotUpdateSerializer, responses={200: PageTextSlotSerializer, **ERRORS}, description="Only `value` is editable; capped at the slot's `max_length`.")
    @action(detail=True, methods=["patch"], url_path=f"text-slots/{SLOT_KEY}")
    def text_slot(self, request, *args, key=None, **kwargs):
        data, expected = _body(TextSlotUpdateSerializer, request)
        slot = content.update_text_slot(self.get_object(), key, user=request.user, data=data, expected_version=expected)
        return Response(PageTextSlotSerializer(slot).data)

    @extend_schema(request=ImageSlotUpdateSerializer, responses={200: PageImageSlotSerializer, **ERRORS}, description="`asset` must be a public image; null keeps the built-in image.")
    @action(detail=True, methods=["patch"], url_path=f"image-slots/{SLOT_KEY}")
    def image_slot(self, request, *args, key=None, **kwargs):
        data, expected = _body(ImageSlotUpdateSerializer, request)
        slot = content.update_image_slot(self.get_object(), key, user=request.user, data=data, expected_version=expected)
        return Response(PageImageSlotSerializer(slot).data)

    @extend_schema(request=None, responses={200: PageSeoSerializer, **READ_ERRORS}, description="The SEO block; unsaved defaults (uid null) until the first edit.")
    @action(detail=True, methods=["get"])
    def seo(self, request, *args, **kwargs):
        return Response(PageSeoSerializer(content.current_seo(self.get_object())).data)

    @extend_schema(request=PageSeoUpdateSerializer, responses={200: PageSeoSerializer, **ERRORS})
    @seo.mapping.patch
    def update_seo(self, request, *args, **kwargs):
        data, expected = _body(PageSeoUpdateSerializer, request)
        seo = content.update_seo(self.get_object(), user=request.user, data=data, expected_version=expected)
        return Response(PageSeoSerializer(content.current_seo(seo.page)).data)

    @extend_schema(request=None, responses={200: PreviewSerializer, **READ_ERRORS}, description="What the website will emit for this page, whatever its status.")
    @action(detail=True, methods=["get"])
    def preview(self, request, *args, **kwargs):
        return Response(delivery.preview(pages.detail(self.get_object())))


class PagePublishActionsMixin(PageViewMixin):
    @extend_schema(request=PageActionSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="DRAFT/ARCHIVED → PUBLISHED.")
    @action(detail=True, methods=["post"])
    def publish(self, request, *args, **kwargs):
        return self.workflow(request, pages.publish)

    @extend_schema(request=PageActionSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="PUBLISHED → DRAFT (the website keeps its built-in copy).")
    @action(detail=True, methods=["post"])
    def unpublish(self, request, *args, **kwargs):
        return self.workflow(request, pages.unpublish)


class PageLifecycleActionsMixin(PageViewMixin):
    @extend_schema(request=PageActionSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="DRAFT/PUBLISHED → ARCHIVED.")
    @action(detail=True, methods=["post"])
    def archive(self, request, *args, **kwargs):
        return self.workflow(request, pages.archive)

    @extend_schema(request=PageActionSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="ARCHIVED → DRAFT.")
    @action(detail=True, methods=["post"])
    def restore(self, request, *args, **kwargs):
        return self.workflow(request, pages.restore)

    @extend_schema(request=PageActionSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="Stamp the current content as reviewed; any later content edit clears it.")
    @action(detail=True, methods=["post"])
    def verify(self, request, *args, **kwargs):
        return self.workflow(request, pages.verify)


@extend_schema_view(
    list=extend_schema(responses={200: PageListSerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer}),
    retrieve=extend_schema(responses={200: PageDetailSerializer, **READ_ERRORS}),
    partial_update=extend_schema(request=PageUpdateSerializer, responses={200: PageDetailSerializer, **ERRORS}, description="List ordering only; status moves through the workflow actions."),
)
class PageViewSet(PageContentActionsMixin, PagePublishActionsMixin, PageLifecycleActionsMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin, BaseViewSet):
    module = "pages"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "seo": "view",
        "preview": "view",
        "partial_update": "edit",
        "text_slot": "edit",
        "image_slot": "edit",
        "update_seo": "edit",
        "publish": "publish",
        "unpublish": "publish",
        "archive": "publish",
        "restore": "publish",
        "verify": "verify",
    }
    services = {"update": pages.update_page}
    http_method_names = ["get", "post", "patch"]
    lookup_value_regex = UUID_REGEX
    filterset_class = PageFilter
    search_fields = ["title", "route", "slug", "description"]
    ordering_fields = ["sort_order", "title", "route", "status", "updated_at"]
    ordering = ["sort_order", "title"]

    def base_queryset(self):
        return pages.pages_queryset() if self.action == "list" else pages.detail_queryset()

    def get_serializer_class(self):
        return {"list": PageListSerializer, "partial_update": PageUpdateSerializer}.get(self.action, PageDetailSerializer)
