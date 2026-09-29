"""``inventory/locations/`` shapes."""

from __future__ import annotations

from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from inventory.models import Location
from inventory.serializers.refs import LiveOfficeField


class LocationSerializer(serializers.ModelSerializer):
    office = LiveOfficeField()

    class Meta:
        model = Location
        fields = ["uid", "code", "name", "office", "created_at", "updated_at", "version"]
        read_only_fields = fields


class LocationCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30, help_text="1-30 letters, digits, '_', '.' or '-'; unique among live locations (case-insensitive).")
    name = serializers.CharField(max_length=150)
    office = serializers.UUIDField(required=False, allow_null=True, help_text="Uid of a live HR office, or null.")

    def to_representation(self, instance):
        return LocationSerializer(instance, context=self.context).data


class LocationUpdateSerializer(ExpectedVersionMixin, LocationCreateSerializer):
    code = serializers.CharField(max_length=30, required=False)
    name = serializers.CharField(max_length=150, required=False)
