"""``content/authors/``, ``content/categories/``, ``content/tags/``, ``content/badges/`` (module ``blogs``)."""

from __future__ import annotations

from blog.serializers.taxonomy import (
    AuthorSerializer,
    AuthorUpdateSerializer,
    AuthorWriteSerializer,
    BadgeSerializer,
    BadgeUpdateSerializer,
    BadgeWriteSerializer,
    CategorySerializer,
    CategoryUpdateSerializer,
    CategoryWriteSerializer,
    TagSerializer,
    TagUpdateSerializer,
    TagWriteSerializer,
)
from blog.services import taxonomy
from blog.views.schema import _CrudViewSet, crud_schema


class _TermViewSet(_CrudViewSet):
    kind: taxonomy.Kind
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "slug", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return taxonomy.queryset(self.kind)


@crud_schema("content_authors", AuthorSerializer, AuthorWriteSerializer, AuthorUpdateSerializer)
class AuthorViewSet(_TermViewSet):
    kind = taxonomy.AUTHOR
    serializer_class = AuthorSerializer
    write_serializers = {"create": AuthorWriteSerializer, "partial_update": AuthorUpdateSerializer}
    services = taxonomy.services_for(taxonomy.AUTHOR)
    search_fields = ["name", "slug", "role"]


@crud_schema("content_categories", CategorySerializer, CategoryWriteSerializer, CategoryUpdateSerializer)
class CategoryViewSet(_TermViewSet):
    kind = taxonomy.CATEGORY
    serializer_class = CategorySerializer
    write_serializers = {"create": CategoryWriteSerializer, "partial_update": CategoryUpdateSerializer}
    services = taxonomy.services_for(taxonomy.CATEGORY)


@crud_schema("content_tags", TagSerializer, TagWriteSerializer, TagUpdateSerializer)
class TagViewSet(_TermViewSet):
    kind = taxonomy.TAG
    serializer_class = TagSerializer
    write_serializers = {"create": TagWriteSerializer, "partial_update": TagUpdateSerializer}
    services = taxonomy.services_for(taxonomy.TAG)


@crud_schema("content_badges", BadgeSerializer, BadgeWriteSerializer, BadgeUpdateSerializer)
class BadgeViewSet(_TermViewSet):
    kind = taxonomy.BADGE
    serializer_class = BadgeSerializer
    write_serializers = {"create": BadgeWriteSerializer, "partial_update": BadgeUpdateSerializer}
    services = taxonomy.services_for(taxonomy.BADGE)
