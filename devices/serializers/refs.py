"""Compact embedded references and the derived health blocks shared by the devices responses."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from devices.models import Agent, Device, SyncLog
from devices.services import health
from hr.serializers.refs import EmployeeRefSerializer, OfficeRefSerializer  # noqa: F401 - re-exported

_at = "at"


def moment(serializer):
    """One ``now`` for a whole response, so every row is judged at the same instant."""
    return (serializer.context or {}).get(_at)


class DeviceHealthSerializer(serializers.Serializer):
    """Derived from timestamps on every read (there is no ``is_online`` column)."""

    status = serializers.ChoiceField(choices=[(value, value) for value in health.DEVICE_STATUSES])
    connection_state = serializers.ChoiceField(choices=[(value, value) for value in health.CONNECTION_STATES])
    connection_label = serializers.CharField()
    connection_note = serializers.CharField()
    transport = serializers.ChoiceField(choices=[(value, value) for value in health.TRANSPORTS])
    transport_label = serializers.CharField()
    adms_state = serializers.ChoiceField(choices=[(value, value) for value in health.ADMS_STATES])
    seconds_since_contact = serializers.IntegerField(allow_null=True)
    is_connected = serializers.BooleanField()
    awaiting_discovery = serializers.BooleanField(help_text="No LAN address yet: an agent must discover the terminal by its serial.")
    display_label = serializers.CharField(help_text="`name · serial` — the registered name first, never the serial instead of it.")
    mapping_consistent = serializers.BooleanField(help_text="False when the agent is filed under another office than the device stands in.")
    mapping_note = serializers.CharField()

    def to_representation(self, instance):
        return super().to_representation(health.device_block(instance, moment(self)))


class AgentRefSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = Agent
        fields = ["uid", "code", "name", "status"]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=[(value, value) for value in health.AGENT_STATUSES]))
    def get_status(self, agent) -> str:
        return health.agent_status(agent, moment(self))


class DeviceRefSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)
    display_label = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["uid", "name", "serial_number", "expected_serial", "office", "is_active", "display_label"]
        read_only_fields = fields

    def get_display_label(self, device) -> str:
        return health.display_label(device)


class DeviceRowSerializer(serializers.ModelSerializer):
    """A terminal in lists that are not about the terminal itself (an agent's devices, the mapping report)."""

    office = OfficeRefSerializer(read_only=True, allow_null=True)
    agent = AgentRefSerializer(read_only=True, allow_null=True)
    health = DeviceHealthSerializer(source="*", read_only=True)

    class Meta:
        model = Device
        fields = [
            "uid",
            "name",
            "serial_number",
            "expected_serial",
            "ip_address",
            "port",
            "office",
            "agent",
            "is_active",
            "adms_enabled",
            "identity_status",
            "last_seen_at",
            "adms_last_seen_at",
            "health",
        ]
        read_only_fields = fields


class SyncLogSerializer(serializers.ModelSerializer):
    device = DeviceRefSerializer(read_only=True, allow_null=True)
    agent = serializers.SlugRelatedField(slug_field="code", read_only=True, allow_null=True, help_text="Agent code.")

    class Meta:
        model = SyncLog
        fields = ["device", "agent", "sync_type", "status", "started_at", "finished_at", "duration_ms", "records_read", "records_new", "records_duplicate", "error_message", "details"]
        read_only_fields = fields


def choices(values) -> list[tuple[str, str]]:
    return [(value, value) for value in values]
