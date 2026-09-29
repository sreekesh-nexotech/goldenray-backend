"""``career-page/`` — the career page (page slug ``career``) maintained through the ``career_page`` grant.

``career_page`` is a registry module of its own (PLAN §3.2; the migrated CMS Career/HR role holds it without ``pages``),
so the same maintenance surface is mounted a second time, pinned to the one page: ``career_page.view``
list/detail/``seo/``/``preview/`` · ``career_page.edit`` slots and SEO · ``career_page.publish`` ``publish/``,
``unpublish/``. No other page is reachable here.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view

from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin
from sitepages.serializers import PageDetailSerializer, PageListSerializer
from sitepages.services import pages
from sitepages.views.pages import READ_ERRORS, UUID_REGEX, PageContentActionsMixin, PagePublishActionsMixin


@extend_schema_view(
    list=extend_schema(responses={200: PageListSerializer(many=True), **READ_ERRORS}, description="The career page (a one-item list)."),
    retrieve=extend_schema(responses={200: PageDetailSerializer, **READ_ERRORS}),
)
class CareerPageViewSet(PageContentActionsMixin, PagePublishActionsMixin, ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "career_page"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "seo": "view",
        "preview": "view",
        "text_slot": "edit",
        "image_slot": "edit",
        "update_seo": "edit",
        "publish": "publish",
        "unpublish": "publish",
    }
    http_method_names = ["get", "post", "patch"]
    lookup_value_regex = UUID_REGEX

    def base_queryset(self):
        queryset = pages.pages_queryset() if self.action == "list" else pages.detail_queryset()
        return queryset.filter(slug=pages.CAREER_PAGE_SLUG)

    def get_serializer_class(self):
        return PageListSerializer if self.action == "list" else PageDetailSerializer
