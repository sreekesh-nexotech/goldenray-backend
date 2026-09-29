"""Staff shapes for ``pages/`` and ``career-page/``. Writes carry only what a maintainer may change."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer
from seo.models import SchemaType
from seo.serializers.api import SeoIssueSerializer
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot
from sitepages.services.pages import default_seo, detail


class UserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    full_name = serializers.CharField(source="get_full_name")


# One ``SeoIssue`` component for every SEO block: the shape is the seo app's (``SeoFields.seo_status()`` issues).


def _public_asset_field(help_text: str):
    return serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), allow_null=True, required=False, help_text=help_text)


class PageTextSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageTextSlot
        fields = ["uid", "key", "label", "kind", "guidance", "value", "max_length", "sort_order", "updated_at", "version"]
        read_only_fields = fields


class PageImageSlotSerializer(serializers.ModelSerializer):
    asset = MediaAssetRefSerializer(read_only=True)
    effective_alt = serializers.CharField(read_only=True)

    class Meta:
        model = PageImageSlot
        fields = ["uid", "key", "label", "guidance", "asset", "external_url", "alt", "effective_alt", "sort_order", "updated_at", "version"]
        read_only_fields = fields


class PageSeoSerializer(serializers.ModelSerializer):
    """The SEO block; ``uid``/``updated_at`` are null until the first edit creates the row (version 1 until then)."""

    uid = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()
    og_image = MediaAssetRefSerializer(read_only=True)
    seo_status = serializers.SerializerMethodField()
    seo_issues = serializers.SerializerMethodField()

    class Meta:
        model = PageSeo
        fields = [
            "uid",
            "seo_title",
            "meta_description",
            "canonical_url",
            "og_title",
            "og_description",
            "og_image",
            "schema_type",
            "schema_extra",
            "noindex",
            "seo_status",
            "seo_issues",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_uid(self, seo) -> str | None:
        return str(seo.uid) if seo.pk else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_updated_at(self, seo):
        return serializers.DateTimeField().to_representation(seo.updated_at) if seo.pk else None

    @extend_schema_field(serializers.ChoiceField(choices=["ok", "warning", "error"]))
    def get_seo_status(self, seo) -> str:
        return seo.seo_status()

    @extend_schema_field(SeoIssueSerializer(many=True))
    def get_seo_issues(self, seo) -> list:
        return seo.seo_issues()


def _seo_of(page: Page) -> PageSeo | None:
    try:
        return page.seo
    except PageSeo.DoesNotExist:
        return None


class PageListSerializer(serializers.ModelSerializer):
    seo_status = serializers.SerializerMethodField()
    image_slot_count = serializers.IntegerField(read_only=True, default=0)
    text_slot_count = serializers.IntegerField(read_only=True, default=0)
    faq_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Page
        fields = [
            "uid",
            "slug",
            "route",
            "title",
            "group",
            "template",
            "status",
            "is_protected",
            "sort_order",
            "seo_status",
            "image_slot_count",
            "text_slot_count",
            "faq_count",
            "verified_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["ok", "warning", "error"]))
    def get_seo_status(self, page) -> str:
        seo = _seo_of(page)
        return (seo or default_seo(page)).seo_status()


class PageDetailSerializer(PageListSerializer):
    """The maintenance view: everything writable (slots, SEO) plus the context around it. No body/copy field."""

    seo = serializers.SerializerMethodField()
    text_slots = PageTextSlotSerializer(many=True, read_only=True)
    image_slots = PageImageSlotSerializer(many=True, read_only=True)
    verified_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta(PageListSerializer.Meta):
        fields = [*PageListSerializer.Meta.fields, "description", "seo", "text_slots", "image_slots", "verified_by", "created_at"]
        read_only_fields = fields

    @extend_schema_field(PageSeoSerializer(allow_null=True))
    def get_seo(self, page):
        seo = _seo_of(page)
        return PageSeoSerializer(seo).data if seo else None


class PageUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    sort_order = serializers.IntegerField(required=False, min_value=-100000, max_value=100000)

    def to_representation(self, instance):
        return PageDetailSerializer(detail(instance), context=self.context).data


class PageActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    """Body of the workflow actions (``publish/``, ``unpublish/``, ``archive/``, ``restore/``, ``verify/``)."""


class TextSlotUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    value = serializers.CharField(allow_blank=True, max_length=20000, help_text="Trimmed; empty = the page keeps its built-in text.")


class ImageSlotUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    asset = _public_asset_field("A public image from the media library (uid), or null to keep the built-in image.")
    external_url = serializers.URLField(max_length=1000, required=False, allow_blank=True, help_text="An http(s) image URL, used when no asset is chosen; empty clears it.")
    alt = serializers.CharField(max_length=255, required=False, allow_blank=True)


class PageSeoUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    meta_description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    canonical_url = serializers.URLField(max_length=1000, required=False, allow_blank=True)
    og_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    og_description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    og_image = _public_asset_field("A public image from the media library (uid), or null.")
    schema_type = serializers.ChoiceField(choices=SchemaType.choices, required=False)
    schema_extra = serializers.DictField(required=False, help_text="Only the structured fields the generator cannot infer.")
    noindex = serializers.BooleanField(required=False)


class PreviewSerializer(serializers.Serializer):
    url = serializers.CharField()
    status = serializers.ChoiceField(choices=Page.Status.choices)
    title = serializers.CharField()
    description = serializers.CharField()
    noindex = serializers.BooleanField()
    schema = serializers.JSONField(allow_null=True)
    seo_status = serializers.ChoiceField(choices=["ok", "warning", "error"])
    seo_issues = SeoIssueSerializer(many=True)
    content = serializers.JSONField(help_text="The public payload's `data` for this page, whatever its status.")
