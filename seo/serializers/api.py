"""SEO serializers (HTTP shape only): page metadata, redirects, the overview and the sitemap feed."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers.assets import MediaAssetRefSerializer
from seo.models import PageMetadata, Redirect, SchemaType
from seo.services import metadata as metadata_service


# ── Page metadata ────────────────────────────────────────────────────────────────────────────────────────────────────
class PageMetadataSerializer(serializers.ModelSerializer):
    og_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    path = serializers.SerializerMethodField(help_text="site path of the page ('/' for home)")

    class Meta:
        model = PageMetadata
        fields = ["uid", "page", "path", "title", "description", "keywords", "og_type", "og_image_url", "og_image", "created_at", "updated_at", "version"]
        read_only_fields = fields

    def get_path(self, row) -> str:
        return metadata_service.page_path(row.page)


class PageMetadataWriteSerializer(serializers.Serializer):
    page = serializers.CharField(max_length=210, help_text="route key: 'home', 'about', 'projects/123' (slashes trimmed, lower-cased)")
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    keywords = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False, default=list)
    og_type = serializers.CharField(max_length=32, required=False, default="website")
    og_image_url = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    og_image_uid = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), source="og_image", required=False, allow_null=True)

    def to_representation(self, instance):
        return PageMetadataSerializer(instance, context=self.context).data


class PageMetadataUpdateSerializer(ExpectedVersionMixin, PageMetadataWriteSerializer):
    page = serializers.CharField(max_length=210, required=False)
    title = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    keywords = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)
    og_type = serializers.CharField(max_length=32, required=False)
    og_image_url = serializers.CharField(max_length=500, required=False, allow_blank=True)


class PublicPageMetadataSerializer(serializers.Serializer):
    """The legacy ``/api/metadata/`` item shape (``goldenray.Metadata``) without its integer id."""

    page = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    keywords = serializers.ListField(child=serializers.CharField())
    imageUrl = serializers.SerializerMethodField()  # noqa: N815 - legacy contract
    ogtype = serializers.CharField(source="og_type")

    @extend_schema_field(serializers.CharField())
    def get_imageUrl(self, row) -> str:  # noqa: N802 - legacy contract
        return metadata_service.image_url(row)


# ── Redirects ────────────────────────────────────────────────────────────────────────────────────────────────────────
class RedirectSerializer(serializers.ModelSerializer):
    permanent = serializers.BooleanField(read_only=True)

    class Meta:
        model = Redirect
        fields = ["uid", "from_path", "to_path", "status_code", "permanent", "hits", "note", "created_at", "updated_at", "version"]
        read_only_fields = fields


class RedirectWriteSerializer(serializers.Serializer):
    from_path = serializers.CharField(max_length=500)
    to_path = serializers.CharField(max_length=1000)
    status_code = serializers.ChoiceField(choices=Redirect.StatusCode.choices, required=False, default=Redirect.StatusCode.PERMANENT_REDIRECT)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def to_representation(self, instance):
        return RedirectSerializer(instance, context=self.context).data


class RedirectUpdateSerializer(ExpectedVersionMixin, RedirectWriteSerializer):
    from_path = serializers.CharField(max_length=500, required=False)
    to_path = serializers.CharField(max_length=1000, required=False)
    status_code = serializers.ChoiceField(choices=Redirect.StatusCode.choices, required=False)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class PublicRedirectSerializer(serializers.ModelSerializer):
    """What the Next.js build turns into ``redirects()`` entries (``source``/``destination``/``permanent``)."""

    permanent = serializers.BooleanField(read_only=True)

    class Meta:
        model = Redirect
        fields = ["from_path", "to_path", "status_code", "permanent"]
        read_only_fields = fields


# ── Overview ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class SeoIssueSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=["error", "warning"])
    field = serializers.CharField()
    message = serializers.CharField()


class SeoOverviewRowSerializer(serializers.Serializer):
    kind = serializers.CharField(help_text="record type (blog, page, faq, job …)")
    uid = serializers.UUIDField()
    label = serializers.CharField()
    path = serializers.CharField()
    record_status = serializers.CharField()
    seo_title = serializers.CharField()
    meta_description = serializers.CharField()
    schema_type = serializers.ChoiceField(choices=SchemaType.choices)
    noindex = serializers.BooleanField()
    seo_status = serializers.ChoiceField(choices=["error", "warning", "ok"])
    updated_at = serializers.DateTimeField()
    issues = SeoIssueSerializer(many=True)


class SeoOverviewCountsSerializer(serializers.Serializer):
    error = serializers.IntegerField()
    warning = serializers.IntegerField()
    ok = serializers.IntegerField()


class SeoOverviewPageSerializer(serializers.Serializer):
    results = SeoOverviewRowSerializer(many=True)
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    counts = SeoOverviewCountsSerializer(help_text="all rows of the kind filter, by status")
    kinds = serializers.ListField(child=serializers.CharField(), help_text="record types that contribute")


# ── Sitemap ──────────────────────────────────────────────────────────────────────────────────────────────────────────
class SitemapEntrySerializer(serializers.Serializer):
    path = serializers.CharField()
    lastmod = serializers.CharField(allow_null=True, help_text="ISO 8601")
