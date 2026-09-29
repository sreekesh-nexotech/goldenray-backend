"""``pricing/prices/`` and ``pricing/current/`` shapes."""

from rest_framework import serializers

from catalog.models import Component
from pricing.models import CurrentPrice, Price, PriceKind
from pricing.serializers.common import PricingComponentRefSerializer, PricingSupplierRefSerializer
from procurement.models import Supplier


class PriceRowSerializer(serializers.ModelSerializer):
    component = PricingComponentRefSerializer(read_only=True)
    supplier = PricingSupplierRefSerializer(read_only=True, allow_null=True)
    is_current = serializers.BooleanField(read_only=True)

    class Meta:
        model = Price
        fields = [
            "uid",
            "component",
            "kind",
            "amount",
            "currency",
            "gst_inclusive",
            "per_watt",
            "effective_from",
            "effective_to",
            "is_current",
            "source",
            "source_ref",
            "supplier",
            "note",
            "version_key",
            "created_at",
        ]
        read_only_fields = fields


class PriceCreateSerializer(serializers.Serializer):
    component_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.objects.all(), source="component")
    kind = serializers.ChoiceField(choices=[(PriceKind.LIST, "List price"), (PriceKind.LANDED, "Landed cost")], help_text="LANDED also needs pricing_internal.view.")
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    effective_from = serializers.DateField(required=False, help_text="Default today; never in the future, never before the current row's.")
    gst_inclusive = serializers.BooleanField(required=False, default=False)
    per_watt = serializers.DecimalField(max_digits=10, decimal_places=4, min_value=0, required=False, allow_null=True)
    supplier_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Supplier.objects.all(), source="supplier", required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_current_uid = serializers.UUIDField(required=False, allow_null=True, help_text="uid of the current row the client saw (null: none); 409 `stale_version` when it changed.")

    def to_representation(self, instance):
        return PriceRowSerializer(instance, context=self.context).data


class CurrentPriceSerializer(serializers.ModelSerializer):
    component = PricingComponentRefSerializer(read_only=True)
    supplier = PricingSupplierRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = CurrentPrice
        fields = ["uid", "component", "kind", "amount", "currency", "gst_inclusive", "per_watt", "effective_from", "source", "source_ref", "supplier", "version_key", "created_at"]
        read_only_fields = fields
