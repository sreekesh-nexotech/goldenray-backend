"""Reference shapes shared by the site-inspection serializers. Nothing here carries a price (engineer boundary)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from customers.serializers.customers import UserRefSerializer
from media.services import signing

__all__ = ["ReadinessBlockerSerializer", "ComponentRefSerializer", "CustomerRefSerializer", "ReadinessSerializer", "SignedPhotoSerializer", "UserRefSerializer", "photo_urls"]


@extend_schema_serializer(component_name="SiteInspectionCustomerRef")
class CustomerRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    phone = serializers.CharField(source="phone_e164", read_only=True)


@extend_schema_serializer(component_name="SiteInspectionComponentRef")
class ComponentRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    sku = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class SignedPhotoSerializer(serializers.Serializer):
    """A private photo: 10-minute signed URLs, never a storage key."""

    url = serializers.CharField()
    thumbnail_url = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


def photo_urls(asset, context) -> dict:
    request = context.get("request")
    version = getattr(request, "version", None) or "v1"
    signed = signing.signed_url(asset, version=version, absolute=request.build_absolute_uri if request is not None else None)
    return {"url": signed.url, "thumbnail_url": signed.thumbnail_url, "expires_at": signed.expires_at}


@extend_schema_field(SignedPhotoSerializer)
class SignedPhotoField(serializers.Field):
    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        kwargs.setdefault("source", "asset")
        super().__init__(**kwargs)

    def to_representation(self, asset):
        return photo_urls(asset, self.context)


class ReadinessBlockerSerializer(serializers.Serializer):
    code = serializers.CharField()
    text = serializers.CharField()
    step = serializers.IntegerField(allow_null=True)
    details = serializers.DictField()


class _FieldCompletionSerializer(serializers.Serializer):
    ready = serializers.BooleanField()
    blockers = ReadinessBlockerSerializer(many=True)
    warnings = ReadinessBlockerSerializer(many=True)
    suitability = serializers.CharField(allow_null=True)


class ReadinessSerializer(_FieldCompletionSerializer):
    """``engines.inspection_readiness``: blocker codes (clients branch on ``code``, never on ``text``)."""

    checks = serializers.DictField(child=serializers.BooleanField())
    approved_location_number = serializers.IntegerField(allow_null=True)
    field_completion = _FieldCompletionSerializer()
