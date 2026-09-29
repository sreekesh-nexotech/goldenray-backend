"""Compact embedded references used across the inventory responses (named ``Inventory*Ref`` in OpenAPI)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import User
from catalog.models import Component
from inventory.models import Location
from inventory.services.locations import live_office


class InventoryComponentRefSerializer(serializers.ModelSerializer):
    unit = serializers.CharField(source="effective_unit", read_only=True, help_text="Unit of qty (NOS, M, KG, SET).")

    class Meta:
        model = Component
        fields = ["uid", "sku", "name", "unit"]
        read_only_fields = fields


class InventoryOfficeRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


@extend_schema_field(InventoryOfficeRefSerializer(allow_null=True))
class LiveOfficeField(serializers.Field):
    """The HR office of a location (the instance itself, or ``source=`` the attribute holding the location) — ``null``
    when it has none or the office was soft-deleted."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        kwargs.setdefault("source", "*")
        super().__init__(**kwargs)

    def to_representation(self, location):
        office = live_office(location)
        return None if office is None else {"uid": str(office.uid), "code": office.code, "name": office.name}


class InventoryLocationRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["uid", "code", "name"]
        read_only_fields = fields


class InventoryUserRefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["uid", "full_name"]
        read_only_fields = fields
