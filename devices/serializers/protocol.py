"""Protocol-mapping and ADMS shapes (staff ``devices/protocol-mappings/``, ``devices/adms/…``)."""

from __future__ import annotations

from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from devices.models import AdmsRequest, AdmsUnknownDevice, ProtocolMapping
from devices.serializers.refs import DeviceRefSerializer, DeviceRowSerializer


class ProtocolMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProtocolMapping
        fields = ["uid", "device_platform", "firmware_version", "field", "raw_value", "meaning_type", "meaning_code", "label", "confidence", "notes", "created_at", "updated_at", "version"]
        read_only_fields = fields


class ProtocolMappingCreateSerializer(serializers.Serializer):
    device_platform = serializers.CharField(max_length=120, required=False, allow_blank=True, default="", help_text="Empty = any platform.")
    firmware_version = serializers.CharField(max_length=120, required=False, allow_blank=True, default="", help_text="Empty = any firmware.")
    field = serializers.ChoiceField(choices=ProtocolMapping.Field.choices)
    raw_value = serializers.IntegerField(min_value=-2147483648, max_value=2147483647)
    meaning_type = serializers.ChoiceField(choices=ProtocolMapping.MeaningType.choices)
    meaning_code = serializers.RegexField(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", max_length=40, help_text="FACE, CARD, FINGERPRINT, PASSWORD, IN, OUT, … (stored upper-case).")
    label = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    confidence = serializers.ChoiceField(choices=ProtocolMapping.Confidence.choices, required=False, default=ProtocolMapping.Confidence.UNKNOWN)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def to_representation(self, instance):
        return ProtocolMappingSerializer(instance, context=self.context).data


class ProtocolMappingUpdateSerializer(ExpectedVersionMixin, ProtocolMappingCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields


class ObservedCodeSerializer(serializers.Serializer):
    raw_value = serializers.IntegerField(allow_null=True)
    count = serializers.IntegerField()
    device_platform = serializers.CharField(allow_blank=True)
    firmware_version = serializers.CharField(allow_blank=True)
    mapped_to = serializers.CharField(allow_null=True)
    confidence = serializers.CharField(allow_null=True)
    mapping_uid = serializers.UUIDField(allow_null=True)


class ObservedSerializer(serializers.Serializer):
    available = serializers.BooleanField(help_text="False until the attendance package's punch store is installed.")
    status = ObservedCodeSerializer(many=True)
    punch = ObservedCodeSerializer(many=True)


class AdmsRequestSerializer(serializers.ModelSerializer):
    device = DeviceRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = AdmsRequest
        fields = [
            "received_at",
            "client_ip",
            "method",
            "path",
            "raw_query",
            "device_serial",
            "device",
            "request_kind",
            "table_name",
            "content_type",
            "body_bytes",
            "body_text",
            "body_encoding",
            "body_truncated",
            "response_status",
            "response_body",
            "records_parsed",
            "records_new",
            "records_duplicate",
            "records_invalid",
            "parse_error",
            "extra",
        ]
        read_only_fields = fields


class AdmsUnknownDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdmsUnknownDevice
        fields = ["uid", "serial_number", "first_seen_at", "last_seen_at", "request_count", "last_source_ip", "last_path", "last_body_excerpt", "last_reason", "notes"]
        read_only_fields = fields


class AdmsStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(help_text="The ADMS_RECEIVER flag (off: /iclock/ answers 404).")
    device_path = serializers.CharField(help_text="What a terminal's push server path looks like (the token is per device).")
    public_base_url = serializers.CharField(allow_blank=True)
    online_seconds = serializers.IntegerField()
    offline_seconds = serializers.IntegerField()
    retention_days = serializers.IntegerField()
    requests_last_24h = serializers.IntegerField()
    unknown_devices = serializers.IntegerField()
    missing_partitions = serializers.ListField(child=serializers.CharField(), help_text="Monthly evidence partitions still to create (rows land in the default partition meanwhile).")
    devices = DeviceRowSerializer(many=True, help_text="Devices expected to push (adms_enabled).")
    devices_truncated = serializers.BooleanField()
