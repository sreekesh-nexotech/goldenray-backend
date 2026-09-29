"""Serializers of ``packs/config-versions/`` (HTTP shape only)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from packs.models import ConfigLine, ConfigPack, ConfigPin, ConfigVersion, LineSource, Phase, SystemType, Tier
from pricing.services.common import can_see_internal

INTERNAL_SECTIONS = ("costs", "pricing")


class PacksUserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)


class ConfigVersionSerializer(serializers.ModelSerializer):
    based_on_number = serializers.IntegerField(source="based_on.number", read_only=True, allow_null=True, default=None)
    submitted_by = PacksUserRefSerializer(read_only=True, allow_null=True)
    approved_by = PacksUserRefSerializer(read_only=True, allow_null=True)
    rejected_by = PacksUserRefSerializer(read_only=True, allow_null=True)
    has_config = serializers.SerializerMethodField()

    class Meta:
        model = ConfigVersion
        fields = [
            "uid",
            "number",
            "status",
            "based_on_number",
            "has_config",
            "note",
            "submitted_by",
            "submitted_at",
            "approved_by",
            "approved_at",
            "rejected_by",
            "rejected_at",
            "rejection_reason",
            "superseded_at",
            "legacy_actor",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_has_config(self, obj) -> bool:
        present = getattr(obj, "config_present", None)
        return present if present is not None else obj.config is not None


class ConfigVersionDetailSerializer(ConfigVersionSerializer):
    config = serializers.SerializerMethodField(help_text=f"The configuration; {', '.join(INTERNAL_SECTIONS)} are null without pricing_internal.view.")
    change_log = serializers.JSONField(read_only=True)

    class Meta(ConfigVersionSerializer.Meta):
        fields = [*ConfigVersionSerializer.Meta.fields, "config", "change_log"]
        read_only_fields = fields

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_config(self, obj):
        if obj.config is None:
            return None
        request = self.context.get("request")
        if can_see_internal(getattr(request, "user", None)):
            return obj.config
        # Cost rates and the margin are internal pricing data (PLAN §3.2 pricing_internal; Flarize refused these reads to Sales).
        return {section: (None if section in INTERNAL_SECTIONS else value) for section, value in obj.config.items()}


class ConfigVersionCreateSerializer(serializers.Serializer):
    based_on_uid = serializers.UUIDField(required=False, help_text="The version to copy (default: the current approved one).")
    config = serializers.JSONField(required=False, help_text="A whole configuration (only when no version is copied).")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate(self, attrs):
        uid = attrs.pop("based_on_uid", None)
        if uid is not None:
            based_on = ConfigVersion.objects.filter(uid=uid).first()
            if based_on is None:
                raise serializers.ValidationError({"based_on_uid": ["Unknown config version."]})
            attrs["based_on"] = based_on
        return attrs


class ConfigVersionUpdateSerializer(serializers.Serializer):
    config = serializers.JSONField(required=False, help_text="Replaces the whole configuration.")
    sections = serializers.DictField(child=serializers.JSONField(), required=False, help_text="Replaces the named sections (bomTemplates, marketRates …).")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        if attrs.get("config") is None and not attrs.get("sections"):
            raise serializers.ValidationError({"config": ["Send `config` or `sections`."]})
        return attrs


class TransitionSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(required=False, min_value=1)


class ApproveSerializer(TransitionSerializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    direct = serializers.BooleanField(required=False, default=False, help_text="Approve a DRAFT in one step (recorded as submitted and approved by you).")


class RejectSerializer(TransitionSerializer):
    reason = serializers.CharField(max_length=2000)


class PackComponentRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    sku = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class ConfigLineSerializer(serializers.ModelSerializer):
    component = PackComponentRefSerializer(read_only=True, allow_null=True)
    source = serializers.ChoiceField(choices=LineSource.choices, read_only=True)

    class Meta:
        model = ConfigLine
        fields = ["slot_key", "component", "name", "qty", "source", "selection_method"]
        read_only_fields = fields


class ConfigPinSerializer(serializers.ModelSerializer):
    component = PackComponentRefSerializer(read_only=True)
    alternates = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = ConfigPin
        fields = ["slot_key", "component", "authoritative", "alternates"]
        read_only_fields = fields


class ConfigPackSerializer(serializers.ModelSerializer):
    system_type = serializers.ChoiceField(choices=SystemType.choices, read_only=True)
    tier = serializers.ChoiceField(choices=Tier.choices, read_only=True)
    phase = serializers.ChoiceField(choices=Phase.choices, read_only=True)
    panel = PackComponentRefSerializer(read_only=True, allow_null=True)
    inverter = PackComponentRefSerializer(read_only=True, allow_null=True)
    battery = PackComponentRefSerializer(read_only=True, allow_null=True)
    pair_of_key = serializers.CharField(source="pair_of.key", read_only=True, allow_null=True, default=None)
    pins = serializers.SerializerMethodField()
    lines = serializers.SerializerMethodField()

    class Meta:
        model = ConfigPack
        fields = [
            "uid",
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
            "pair_of_key",
            "panel",
            "inverter",
            "battery",
            "battery_qty",
            "profile_key",
            "market_rate_key",
            "build_error",
            "pins",
            "lines",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(ConfigPinSerializer(many=True))
    def get_pins(self, obj) -> list[dict]:
        return ConfigPinSerializer([pin for pin in obj.pins.all() if pin.deleted_at is None], many=True).data

    @extend_schema_field(ConfigLineSerializer(many=True))
    def get_lines(self, obj) -> list[dict]:
        return ConfigLineSerializer(obj.lines.all(), many=True).data


class PinsSerializer(serializers.Serializer):
    pins = serializers.DictField(child=serializers.UUIDField(allow_null=True), help_text="{slot_key: component uid | null}; replaces the pack's pins.")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1, help_text="The config version's `version`.")
