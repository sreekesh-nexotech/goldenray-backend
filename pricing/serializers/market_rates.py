"""Shapes of ``pricing/market-rate-sets/`` and its ``rates/``, ``swap-deltas/``, ``roof-addons/`` children."""

from decimal import Decimal

from rest_framework import serializers

from catalog.models import Component
from core.serializers.common import ExpectedVersionMixin
from pricing.models import MarketRate, MarketRateSet, RoofAddon, SwapDelta
from pricing.serializers.common import PricingComponentRefSerializer
from pricing.services.market_rates import rate_key


class MarketRateSetSerializer(serializers.ModelSerializer):
    rate_count = serializers.IntegerField(read_only=True, default=0)
    swap_delta_count = serializers.IntegerField(read_only=True, default=0)
    roof_addon_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = MarketRateSet
        fields = ["uid", "name", "status", "activated_at", "retired_at", "note", "rate_count", "swap_delta_count", "roof_addon_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class MarketRateSetWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    note = serializers.CharField(required=False, allow_blank=True)
    copy_from_uid = serializers.SlugRelatedField(
        slug_field="uid", queryset=MarketRateSet.objects.all(), source="copy_from", required=False, allow_null=True, help_text="Copy every rate, swap delta and roof add-on of this set."
    )

    def to_representation(self, instance):
        return MarketRateSetSerializer(instance, context=self.context).data


class MarketRateSetUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    note = serializers.CharField(required=False, allow_blank=True)

    def to_representation(self, instance):
        return MarketRateSetSerializer(instance, context=self.context).data


class ActivateSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)


class MarketRateSerializer(serializers.ModelSerializer):
    key = serializers.SerializerMethodField(help_text="Flarize market-rate key: ongrid_value, hybrid_base_1_up10, upgrade_3_5 …")

    class Meta:
        model = MarketRate
        fields = [
            "uid",
            "key",
            "system_type",
            "tier",
            "battery_config",
            "size_key",
            "size_kw",
            "phase",
            "from_size_key",
            "future_size_key",
            "variant",
            "customer_price_incl_gst",
            "sort_order",
        ]
        read_only_fields = fields

    def get_key(self, rate) -> str:
        return rate_key(rate)


class MarketRateInputSerializer(serializers.Serializer):
    system_type = serializers.ChoiceField(choices=MarketRate._meta.get_field("system_type").choices)
    tier = serializers.ChoiceField(choices=[("", "None (upgrade)"), ("BASE", "Base"), ("VALUE", "Value"), ("PREMIUM", "Premium")], required=False, allow_blank=True, default="")
    battery_config = serializers.ChoiceField(choices=MarketRate._meta.get_field("battery_config").choices, required=False, allow_blank=True, default="")
    size_key = serializers.CharField(max_length=8, help_text="3, 5sp (5 kW 1P), 5tp (5 kW 3P), 10 … — for UPGRADE the target size.")
    from_size_key = serializers.CharField(max_length=8, required=False, allow_blank=True, default="", help_text="UPGRADE only: the starting size.")
    future_size_key = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")
    variant = serializers.CharField(max_length=24, required=False, allow_blank=True, default="")
    customer_price_incl_gst = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, help_text="0 = not set.")
    sort_order = serializers.IntegerField(min_value=0, required=False)


class MarketRatesPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    rates = MarketRateInputSerializer(many=True)


class ReplaceOutcomeSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    deleted = serializers.IntegerField()


class ReplaceResultSerializer(serializers.Serializer):
    set = MarketRateSetSerializer()
    outcome = ReplaceOutcomeSerializer()


class SwapDeltaSerializer(serializers.ModelSerializer):
    from_component = PricingComponentRefSerializer(read_only=True)
    to_component = PricingComponentRefSerializer(read_only=True)

    class Meta:
        model = SwapDelta
        fields = ["uid", "system_type", "tier", "slot", "from_component", "to_component", "delta_incl_gst"]
        read_only_fields = fields


class SwapDeltaInputSerializer(serializers.Serializer):
    system_type = serializers.ChoiceField(choices=SwapDelta._meta.get_field("system_type").choices)
    tier = serializers.ChoiceField(choices=SwapDelta._meta.get_field("tier").choices)
    slot = serializers.ChoiceField(choices=SwapDelta._meta.get_field("slot").choices)
    from_component_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.objects.all(), source="from_component")
    to_component_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.objects.all(), source="to_component")
    delta_incl_gst = serializers.DecimalField(max_digits=14, decimal_places=2)


class SwapDeltasPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    swap_deltas = SwapDeltaInputSerializer(many=True)


class RoofAddonSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoofAddon
        fields = ["uid", "structure_type", "size_kw", "addon_incl_gst"]
        read_only_fields = fields


class RoofAddonInputSerializer(serializers.Serializer):
    structure_type = serializers.ChoiceField(choices=RoofAddon._meta.get_field("structure_type").choices)
    size_kw = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"))
    addon_incl_gst = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)


class RoofAddonsPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    roof_addons = RoofAddonInputSerializer(many=True)
