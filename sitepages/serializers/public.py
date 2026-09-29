"""OpenAPI description of the public page payload (built by ``sitepages.services.delivery``; legacy contract)."""

from rest_framework import serializers


class PublicImageSerializer(serializers.Serializer):
    url = serializers.URLField()
    alt = serializers.CharField(allow_blank=True)
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)


class PublicPageSeoSerializer(serializers.Serializer):
    title = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    canonical_url = serializers.CharField(allow_blank=True)
    noindex = serializers.BooleanField()
    og_image = PublicImageSerializer(allow_null=True, help_text="Social share image, or null to keep the shipped one.")
    schema = serializers.JSONField(allow_null=True, help_text="WebPage JSON-LD generated from the record, or null.")


class PublicPageDataSerializer(serializers.Serializer):
    route = serializers.CharField()
    name = serializers.CharField()
    images = serializers.DictField(child=PublicImageSerializer(allow_null=True), help_text="slot key → replaced image, or null (the shipped image applies).")
    text = serializers.DictField(child=serializers.CharField(), help_text="slot key → corrected text; absent when not overridden.")
    seo = PublicPageSeoSerializer(allow_null=True, help_text="Null until the page's SEO was first edited.")


class PublicPageContentSerializer(serializers.Serializer):
    data = PublicPageDataSerializer()


class PublicPageQuerySerializer(serializers.Serializer):
    """Query of ``pages/?route=``: validated (no NUL bytes, at most the column's length) but never trimmed — the
    route matches exactly, as the legacy ``page-content?route=`` did."""

    route = serializers.CharField(max_length=255, trim_whitespace=False, help_text="Site path of the page, e.g. `/career`.")
