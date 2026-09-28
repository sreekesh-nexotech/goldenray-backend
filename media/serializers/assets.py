"""Media asset shapes. The storage key of a private file is never serialised; private files have no URL here —
clients ask ``media/<uid>/signed-url/`` for a short-lived one."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from media.models import MediaAsset
from media.services.storage import sibling_public_url


class MediaAssetSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField(help_text="CDN URL of a public file; null for private files (use signed-url/).")
    thumbnail_url = serializers.SerializerMethodField(help_text="CDN URL of a public file's thumbnail; null otherwise.")
    uploaded_by = serializers.SlugRelatedField(slug_field="uid", read_only=True, help_text="Uploader's user uid.")

    class Meta:
        model = MediaAsset
        fields = [
            "uid",
            "visibility",
            "kind",
            "url",
            "thumbnail_url",
            "original_filename",
            "mime_type",
            "size_bytes",
            "width",
            "height",
            "captured_at",
            "checksum_sha256",
            "alternative_text",
            "caption",
            "folder",
            "uploaded_by",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_url(self, asset: MediaAsset) -> str | None:
        return (asset.cdn_url or None) if asset.is_public else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_thumbnail_url(self, asset: MediaAsset) -> str | None:
        if not asset.is_public or not asset.thumbnail_key:
            return None
        return sibling_public_url(asset.cdn_url, asset.file, asset.thumbnail_key)


class MediaAssetRefSerializer(serializers.ModelSerializer):
    """Compact embedded reference (other apps' responses): public URL only, never a private key."""

    url = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields = ["uid", "kind", "visibility", "url", "alternative_text", "width", "height", "mime_type"]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_url(self, asset: MediaAsset) -> str | None:
        return (asset.cdn_url or None) if asset.is_public else None


class MediaUploadSerializer(serializers.Serializer):
    file = serializers.FileField(allow_empty_file=True, use_url=False, help_text="The file; its type is detected from the content.")
    visibility = serializers.ChoiceField(choices=MediaAsset.Visibility.choices)
    kind = serializers.ChoiceField(choices=MediaAsset.Kind.choices)
    folder = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    alternative_text = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    caption = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class MediaAssetUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    alternative_text = serializers.CharField(max_length=255, required=False, allow_blank=True)
    caption = serializers.CharField(max_length=2000, required=False, allow_blank=True)

    def to_representation(self, instance):
        return MediaAssetSerializer(instance, context=self.context).data


class SignedUrlSerializer(serializers.Serializer):
    url = serializers.CharField(help_text="Download URL (signed and short-lived for private files).")
    thumbnail_url = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True, help_text="Null for public files (their CDN URL does not expire).")
