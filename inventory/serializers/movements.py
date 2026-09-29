"""``inventory/movements/`` and ``inventory/balances/`` shapes."""

from __future__ import annotations

from rest_framework import serializers

from inventory.models import Balance, Direction, Movement, Reason
from inventory.serializers.refs import InventoryComponentRefSerializer, InventoryLocationRefSerializer, InventoryUserRefSerializer, LiveOfficeField


class MovementSerializer(serializers.ModelSerializer):
    component = InventoryComponentRefSerializer(read_only=True)
    location = InventoryLocationRefSerializer(read_only=True)
    by = InventoryUserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Movement
        fields = ["uid", "component", "location", "qty", "direction", "reason", "ref_type", "ref_uid", "at", "by", "note", "created_at"]
        read_only_fields = fields


class MovementRecordedSerializer(MovementSerializer):
    balance_after = serializers.DecimalField(max_digits=18, decimal_places=3, read_only=True, help_text="Stock of the component at the location after this movement.")
    negative_override = serializers.BooleanField(read_only=True, help_text="True when an ADJUST booked a negative balance.")

    class Meta(MovementSerializer.Meta):
        fields = [*MovementSerializer.Meta.fields, "balance_after", "negative_override"]
        read_only_fields = fields


class MovementCreateSerializer(serializers.Serializer):
    component = serializers.UUIDField(help_text="Uid of a live catalog component.")
    location = serializers.UUIDField(help_text="Uid of a live inventory location.")
    qty = serializers.DecimalField(max_digits=12, decimal_places=3, help_text="Positive, at most 3 decimals; the direction gives the sign.")
    direction = serializers.ChoiceField(choices=Direction.choices)
    reason = serializers.ChoiceField(choices=Reason.choices, help_text="PURCHASE is IN, ISSUE_TO_PROJECT is OUT, RETURN and ADJUST either way.")
    ref_type = serializers.CharField(max_length=64, required=False, allow_blank=True, help_text="<app>.<model> of what caused it (required for ISSUE_TO_PROJECT).")
    ref_uid = serializers.UUIDField(required=False, allow_null=True)
    at = serializers.DateTimeField(required=False, help_text="When the stock moved (default now; never in the future).")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000, help_text="Required for ADJUST.")

    def to_representation(self, instance):
        return MovementRecordedSerializer(instance, context=self.context).data


class BalanceSerializer(serializers.ModelSerializer):
    component = InventoryComponentRefSerializer(read_only=True)
    location = InventoryLocationRefSerializer(read_only=True)
    office = LiveOfficeField(source="location", help_text="The location's HR office.")

    class Meta:
        model = Balance
        fields = ["component", "location", "office", "qty", "qty_in", "qty_out", "movement_count", "last_movement_at"]
        read_only_fields = fields
