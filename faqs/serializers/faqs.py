"""Staff shapes for ``faqs/`` and ``faq-categories/``; OpenAPI shapes for the public ``faqs/`` payload."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from faqs.models import Faq, FaqCategory
from faqs.services.faqs import publish_errors
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer
from seo.models import SchemaType
from sitepages.models import Page
from sitepages.serializers import SeoIssueSerializer, UserRefSerializer


# ── Categories ──────────────────────────────────────────────────────────────────────────────────────────────────────
class FaqCategorySerializer(serializers.ModelSerializer):
    faq_count = serializers.IntegerField(read_only=True, default=0, help_text="Live FAQs (any status) using it.")
    published_faq_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = FaqCategory
        fields = ["uid", "name", "slug", "description", "is_active", "sort_order", "faq_count", "published_faq_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class FaqCategoryCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    slug = serializers.CharField(max_length=120, required=False, allow_blank=True, help_text="Derived from the name when blank.")
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False, default=0, min_value=-100000, max_value=100000)

    def to_representation(self, instance):
        return FaqCategorySerializer(instance, context=self.context).data


class FaqCategoryUpdateSerializer(ExpectedVersionMixin, FaqCategoryCreateSerializer):
    name = serializers.CharField(max_length=120, required=False)
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, min_value=-100000, max_value=100000)


# ── FAQs ────────────────────────────────────────────────────────────────────────────────────────────────────────────
class PageRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Page
        fields = ["uid", "slug", "route", "title", "status"]
        read_only_fields = fields


# The component name carries the app: catalog (product master) has its own ``CategoryRef``.
@extend_schema_serializer(component_name="FaqCategoryRef")
class CategoryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = FaqCategory
        fields = ["uid", "name", "slug", "is_active"]
        read_only_fields = fields


class FaqListSerializer(serializers.ModelSerializer):
    """The FAQ list row: page and category inlined, SEO indicator, who touched it last."""

    page = PageRefSerializer(read_only=True, allow_null=True)
    category = CategoryRefSerializer(read_only=True, allow_null=True)
    seo_status = serializers.SerializerMethodField()
    updated_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Faq
        fields = [
            "uid",
            "question",
            "page",
            "section",
            "category",
            "status",
            "sort_order",
            "seo_status",
            "published_at",
            "archived_at",
            "verified_at",
            "updated_by",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["ok", "warning", "error"]))
    def get_seo_status(self, faq) -> str:
        return faq.seo_status()


class FaqSerializer(FaqListSerializer):
    """The editor shape: content, SEO block, what blocks publishing right now, review and attribution."""

    og_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    seo_issues = serializers.SerializerMethodField()
    publish_errors = serializers.SerializerMethodField(help_text="Empty when Publish is allowed.")
    created_by = UserRefSerializer(read_only=True, allow_null=True)
    verified_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta(FaqListSerializer.Meta):
        fields = [
            *FaqListSerializer.Meta.fields,
            "answer",
            "seo_title",
            "meta_description",
            "canonical_url",
            "og_title",
            "og_description",
            "og_image",
            "schema_type",
            "schema_extra",
            "noindex",
            "seo_issues",
            "publish_errors",
            "created_by",
            "verified_by",
        ]
        read_only_fields = fields

    @extend_schema_field(SeoIssueSerializer(many=True))
    def get_seo_issues(self, faq) -> list:
        return faq.seo_issues()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_publish_errors(self, faq) -> list:
        return publish_errors(faq)


def _page_field(**kwargs):
    return serializers.SlugRelatedField(slug_field="uid", queryset=Page.objects.all(), help_text="Page uid.", **kwargs)


class _FaqWriteSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=500)
    answer = serializers.CharField(required=False, allow_blank=True, max_length=20000, help_text="Plain text or light HTML (trimmed); may be blank in a draft.")
    page = _page_field(allow_null=True, required=False)
    section = serializers.CharField(max_length=80, required=False, allow_blank=True)
    category = serializers.SlugRelatedField(slug_field="uid", queryset=FaqCategory.objects.all(), allow_null=True, required=False, help_text="Category uid.")
    sort_order = serializers.IntegerField(required=False, min_value=0, max_value=100000, help_text="Defaults to the end of the page/section.")
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    meta_description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    canonical_url = serializers.URLField(max_length=1000, required=False, allow_blank=True)
    og_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    og_description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    og_image = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), allow_null=True, required=False, help_text="A public image (uid).")
    schema_type = serializers.ChoiceField(choices=SchemaType.choices, required=False)
    schema_extra = serializers.DictField(required=False)
    noindex = serializers.BooleanField(required=False)

    def to_representation(self, instance):
        return FaqSerializer(instance, context=self.context).data


class FaqCreateSerializer(_FaqWriteSerializer):
    pass


class FaqUpdateSerializer(ExpectedVersionMixin, _FaqWriteSerializer):
    question = serializers.CharField(max_length=500, required=False)


class FaqActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    """Body of ``publish/``, ``unpublish/``, ``archive/``, ``restore/``, ``verify/``."""


class FaqReorderSerializer(serializers.Serializer):
    page = _page_field()
    section = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    order = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=500, help_text="FAQ uids in the new order; unknown uids are ignored.")

    def validate_order(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Each FAQ may appear only once.")
        return value


class FaqPreviewSerializer(serializers.Serializer):
    question = serializers.CharField()
    answer = serializers.CharField()
    status = serializers.ChoiceField(choices=Faq.Status.choices)
    page = serializers.JSONField(allow_null=True)
    section = serializers.CharField()
    position = serializers.IntegerField()
    url = serializers.CharField(allow_null=True)
    schema = serializers.JSONField(allow_null=True, help_text="FAQPage JSON-LD of the section this FAQ joins.")
    publish_errors = serializers.ListField(child=serializers.CharField())
    seo_status = serializers.ChoiceField(choices=["ok", "warning", "error"])
    seo_issues = SeoIssueSerializer(many=True)


# ── Public payload (OpenAPI only; built by faqs.services.delivery) ─────────────────────────────────────────────────
class PublicFaqSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The FAQ's uid.")
    question = serializers.CharField()
    answer = serializers.CharField()
    section = serializers.CharField(allow_blank=True)
    category = serializers.CharField(allow_null=True)
    order = serializers.IntegerField()


class PublicFaqPageSerializer(serializers.Serializer):
    name = serializers.CharField()
    route = serializers.CharField()


class PublicFaqMetaSerializer(serializers.Serializer):
    page = PublicFaqPageSerializer()
    count = serializers.IntegerField()
    schema = serializers.JSONField(allow_null=True, help_text="FAQPage JSON-LD built from the returned rows.")


class PublicFaqListSerializer(serializers.Serializer):
    data = PublicFaqSerializer(many=True)
    meta = PublicFaqMetaSerializer()


class PublicFaqQuerySerializer(serializers.Serializer):
    """Query of the public ``faqs/``: validated (no NUL bytes, bounded) but never trimmed — values match exactly as in the
    legacy ``/api/faqs`` (``section=`` present and empty selects the unnamed section)."""

    route = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, max_length=255)
    page = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, max_length=255)
    section = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, max_length=80)
    category = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, max_length=120)
