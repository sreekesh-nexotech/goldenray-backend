"""Website payloads (``/api/public/v1/packs/``): customer prices and a BOM summary — no cost or margin fields."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from packs.models import Phase, ReleasePack, SystemType, Tier
from packs.services.public import bom_summary


class PublicBomLineSerializer(serializers.Serializer):
    category = serializers.CharField(allow_null=True)
    name = serializers.CharField(allow_null=True)
    qty = serializers.JSONField()


class PublicPackSerializer(serializers.ModelSerializer):
    system_type = serializers.ChoiceField(choices=SystemType.choices, read_only=True)
    tier = serializers.ChoiceField(choices=Tier.choices, read_only=True)
    phase = serializers.ChoiceField(choices=Phase.choices, read_only=True)
    is_future_ready = serializers.SerializerMethodField()
    release_number = serializers.IntegerField(source="release.number", read_only=True)
    bom = serializers.SerializerMethodField()

    class Meta:
        model = ReleasePack
        fields = [
            "key",
            "display_name",
            "system_type",
            "tier",
            "size_key",
            "size_kw",
            "phase",
            "battery_config",
            "future_size_key",
            "future_size_kw",
            "is_future_ready",
            "customer_price_incl_gst",
            "customer_price_excl_gst",
            "gst_amount",
            "release_number",
            "bom",
        ]
        read_only_fields = fields

    def get_is_future_ready(self, obj) -> bool:
        return bool(obj.future_size_key)

    @extend_schema_field(PublicBomLineSerializer(many=True))
    def get_bom(self, obj) -> list[dict]:
        return bom_summary(obj.bom)


class PublicPackGroupSerializer(serializers.Serializer):
    system_type = serializers.ChoiceField(choices=SystemType.choices)
    tier = serializers.ChoiceField(choices=Tier.choices)
    size = serializers.CharField()
    packs = PublicPackSerializer(many=True)
