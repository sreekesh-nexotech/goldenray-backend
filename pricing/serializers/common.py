"""Shapes shared by the pricing (and procurement) serializers: references and the ``pricing_internal`` field gate."""

from rest_framework import serializers

from catalog.models import Component


class PricingComponentRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["uid", "sku", "name", "status"]
        read_only_fields = fields


class PricingSupplierRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class PricingUserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    email = serializers.EmailField()


class InternalFieldsMixin:
    """Drops ``internal_fields`` from the output unless ``context["internal"]`` (the caller holds
    ``pricing_internal.view``). The fields stay documented in OpenAPI (optional, with the note)."""

    internal_fields: tuple[str, ...] = ()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("internal", False):
            for name in self.internal_fields:
                data.pop(name, None)
        return data


INTERNAL_NOTE = "Only with pricing_internal.view (omitted otherwise)."


class ReasonSerializer(serializers.Serializer):
    """Body of a workflow action: optional reason + optimistic-locking token."""

    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_version = serializers.IntegerField(min_value=1, required=False, help_text="The `version` the client last read; 409 `stale_version` when outdated.")
