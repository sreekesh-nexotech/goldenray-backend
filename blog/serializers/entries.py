"""Entry shapes for the Studio: slim list rows, the full detail, the one-call write and the workflow actions.

Relations are uids on write and expanded objects on read. Child collections (``content_blocks``, ``images``,
``attribute_values``, ``category_uids`` / ``tag_uids`` / ``badge_uids``) are **replace-all**: sending one replaces
the whole set, omitting it leaves the set untouched.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from blog.models import Author, Badge, Category, Collection, ContentBlock, Entry, EntrySlugHistory, Tag, Template
from blog.serializers.delivery import image_url
from blog.services.entries import live_seo
from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers.assets import MediaAssetRefSerializer
from seo.models import SchemaType


# ── Read ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
class _CollectionRef(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ["uid", "api_uid", "plural_name", "path_prefix"]
        read_only_fields = fields


class _TemplateRef(serializers.ModelSerializer):
    class Meta:
        model = Template
        fields = ["uid", "slug", "name"]
        read_only_fields = fields


class _AuthorRef(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["uid", "name", "slug"]
        read_only_fields = fields


class _TermRef(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()


class _UserRef(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_name(self, user) -> str:
        return f"{user.first_name} {user.last_name}".strip() or user.email


class EntryListSerializer(serializers.ModelSerializer):
    collection = _CollectionRef(read_only=True)
    template = _TemplateRef(read_only=True, allow_null=True)
    author = _AuthorRef(read_only=True, allow_null=True)
    cover_url = serializers.SerializerMethodField(help_text="cover image, else the first resolvable image")

    class Meta:
        model = Entry
        fields = [
            "uid",
            "collection",
            "template",
            "title",
            "slug",
            "excerpt",
            "status",
            "author",
            "cover_url",
            "is_featured",
            "sort_order",
            "locale",
            "published_on",
            "published_at",
            "scheduled_for",
            "archived_at",
            "verified_at",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_cover_url(self, entry) -> str | None:
        cover = entry.cover_image
        if cover is not None and cover.deleted_at is None and cover.is_public and cover.cdn_url:
            return cover.cdn_url
        for image in entry.images.all():
            url = image_url(image)
            if url:
                return url
        return None


class ContentBlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentBlock
        fields = ["uid", "kind", "component", "data", "position"]
        read_only_fields = fields


class EntryImageSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    group_key = serializers.CharField()
    position = serializers.IntegerField()
    media_asset = MediaAssetRefSerializer(allow_null=True)
    external_url = serializers.CharField()
    alt = serializers.CharField()
    url = serializers.SerializerMethodField()

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_url(self, image) -> str | None:
        return image_url(image)


class AttributeValueSerializer(serializers.Serializer):
    slot_key = serializers.CharField(max_length=60)
    value = serializers.JSONField(allow_null=True)


class EntrySeoSerializer(serializers.Serializer):
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    meta_description = serializers.CharField(required=False, allow_blank=True)
    canonical_url = serializers.URLField(max_length=1000, required=False, allow_blank=True)
    og_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    og_description = serializers.CharField(required=False, allow_blank=True)
    og_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    schema_type = serializers.ChoiceField(choices=SchemaType.choices, required=False)
    schema_extra = serializers.JSONField(required=False)
    noindex = serializers.BooleanField(required=False)
    keywords = serializers.CharField(required=False, allow_blank=True, help_text="comma-separated meta keywords")
    seo_status = serializers.SerializerMethodField()
    issues = serializers.SerializerMethodField()

    def get_seo_status(self, seo) -> str:
        return seo.seo_status()

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_issues(self, seo) -> list:
        return seo.seo_issues()


class SlugHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = EntrySlugHistory
        fields = ["uid", "slug", "active", "note", "created_at", "updated_at", "version"]
        read_only_fields = fields


class EntryDetailSerializer(EntryListSerializer):
    categories = _TermRef(many=True, read_only=True)
    tags = _TermRef(many=True, read_only=True)
    badges = _TermRef(many=True, read_only=True)
    cover_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    content_blocks = ContentBlockSerializer(many=True, read_only=True)
    images = EntryImageSerializer(many=True, read_only=True)
    attribute_values = AttributeValueSerializer(many=True, read_only=True)
    seo = serializers.SerializerMethodField()
    slug_history = SlugHistorySerializer(many=True, read_only=True)
    verified_by = _UserRef(read_only=True, allow_null=True)
    path = serializers.CharField(read_only=True, help_text="site path of the entry")

    class Meta(EntryListSerializer.Meta):
        fields = [
            *EntryListSerializer.Meta.fields,
            "path",
            "summary",
            "introduction",
            "warning",
            "insights",
            "read_time",
            "categories",
            "tags",
            "badges",
            "cover_image",
            "content_blocks",
            "images",
            "attribute_values",
            "seo",
            "slug_history",
            "verified_by",
        ]
        read_only_fields = fields

    @extend_schema_field(EntrySeoSerializer(allow_null=True))
    def get_seo(self, entry):
        seo = live_seo(entry)
        return EntrySeoSerializer(seo).data if seo is not None else None


# ── Write ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _media_field(source: str) -> serializers.SlugRelatedField:
    return serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), source=source, required=False, allow_null=True)


class ContentBlockInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=ContentBlock.Kind.choices, required=False, help_text="default RICH_TEXT (or derived from component)")
    component = serializers.CharField(max_length=120, required=False, help_text="delivered __component; default per kind (shared.rich-text …)")
    data = serializers.JSONField(help_text="block body: a Strapi-blocks array for RICH_TEXT")
    position = serializers.IntegerField(required=False)


class EntryImageInputSerializer(serializers.Serializer):
    group_key = serializers.CharField(max_length=60)
    position = serializers.IntegerField(required=False)
    media_asset_uid = _media_field("media_asset")
    external_url = serializers.URLField(max_length=1000, required=False, allow_blank=True)
    alt = serializers.CharField(max_length=255, required=False, allow_blank=True)


class EntrySeoInputSerializer(serializers.Serializer):
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    meta_description = serializers.CharField(required=False, allow_blank=True)
    canonical_url = serializers.URLField(max_length=1000, required=False, allow_blank=True)
    og_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    og_description = serializers.CharField(required=False, allow_blank=True)
    og_image_uid = _media_field("og_image")
    schema_type = serializers.ChoiceField(choices=SchemaType.choices, required=False)
    schema_extra = serializers.JSONField(required=False)
    noindex = serializers.BooleanField(required=False)
    keywords = serializers.CharField(required=False, allow_blank=True)


def _terms(model, source: str) -> serializers.SlugRelatedField:
    return serializers.SlugRelatedField(slug_field="uid", queryset=model.objects.all(), source=source, many=True, required=False)


class EntryWriteSerializer(serializers.Serializer):
    collection_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Collection.objects.all(), source="collection")
    template_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Template.objects.all(), source="template", required=False, allow_null=True)
    title = serializers.CharField(max_length=255)
    slug = serializers.CharField(max_length=255, help_text="lowercase-hyphen public slug; validated and unique in the collection (aliases included)")
    excerpt = serializers.CharField(required=False, allow_blank=True)
    summary = serializers.ListField(child=serializers.DictField(), required=False, help_text="Strapi-blocks array")
    introduction = serializers.ListField(child=serializers.DictField(), required=False, help_text="Strapi-blocks array")
    warning = serializers.CharField(required=False, allow_blank=True)
    insights = serializers.CharField(required=False, allow_blank=True)
    read_time = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=32767)
    is_featured = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, allow_null=True)
    locale = serializers.RegexField(r"^[a-z]{2}$", required=False)
    published_on = serializers.DateTimeField(required=False, allow_null=True, help_text="author-set display date")
    author_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Author.objects.all(), source="author", required=False, allow_null=True)
    cover_image_uid = _media_field("cover_image")
    category_uids = _terms(Category, "categories")
    tag_uids = _terms(Tag, "tags")
    badge_uids = _terms(Badge, "badges")
    content_blocks = ContentBlockInputSerializer(many=True, required=False)
    images = EntryImageInputSerializer(many=True, required=False)
    attribute_values = AttributeValueSerializer(many=True, required=False)
    seo = EntrySeoInputSerializer(required=False, allow_null=True, help_text="null removes the SEO block")

    def to_representation(self, instance):
        return EntryDetailSerializer(instance, context=self.context).data


class EntryUpdateSerializer(ExpectedVersionMixin, EntryWriteSerializer):
    collection_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Collection.objects.all(), source="collection", required=False, help_text="immutable: another value is refused")
    title = serializers.CharField(max_length=255, required=False)
    slug = serializers.CharField(max_length=255, required=False)


# ── Actions ──────────────────────────────────────────────────────────────────────────────────────────────────────────
class EntryActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass


class EntryScheduleSerializer(ExpectedVersionMixin, serializers.Serializer):
    scheduled_for = serializers.DateTimeField(allow_null=True, help_text="future publication time; null cancels the schedule")


class PreviewLinkSerializer(serializers.Serializer):
    token = serializers.CharField()
    url = serializers.CharField(help_text="public preview URL (valid until expires_at)")
    expires_at = serializers.DateTimeField()


class CheckSlugQuerySerializer(serializers.Serializer):
    collection = serializers.CharField(help_text="collection uid or api_uid")
    slug = serializers.CharField(max_length=300, help_text="candidate slug or raw title (slugified server-side)")
    exclude = serializers.UUIDField(required=False, help_text="uid of the entry being edited")


class CheckSlugSerializer(serializers.Serializer):
    slug = serializers.CharField()
    available = serializers.BooleanField()
    suggestion = serializers.CharField()
    valid = serializers.BooleanField()
    error = serializers.CharField(allow_null=True)


class AliasCreateSerializer(serializers.Serializer):
    slug = serializers.CharField(max_length=255, help_text="a verified historical slug of this entry")
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)
