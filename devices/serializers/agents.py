"""Office-agent shapes (staff ``devices/agents/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from devices.models import Agent
from devices.serializers.refs import DeviceRowSerializer, OfficeRefSerializer, choices, moment
from devices.services import health
from hr.models import Office


class AgentSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True, help_text="Where the agent is filed (its devices' offices are the mapping).")
    status = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    seconds_since_heartbeat = serializers.SerializerMethodField()
    token_prefix = serializers.SerializerMethodField(help_text="Tells two credentials apart; the token itself is never shown again.")
    token_issued_at = serializers.SerializerMethodField()
    token_revoked_at = serializers.SerializerMethodField()
    token_last_used_at = serializers.SerializerMethodField()
    devices = DeviceRowSerializer(many=True, read_only=True, help_text="The terminals bound to this agent, each with its own health.")
    device_office_names = serializers.SerializerMethodField()
    office_mismatch = serializers.SerializerMethodField(help_text="A device of this agent stands in another office than the agent is filed under.")

    class Meta:
        model = Agent
        fields = [
            "uid",
            "code",
            "name",
            "office",
            "status",
            "is_online",
            "seconds_since_heartbeat",
            "is_active",
            "token_prefix",
            "token_issued_at",
            "token_revoked_at",
            "token_last_used_at",
            "agent_version",
            "hostname",
            "platform",
            "local_ip",
            "last_heartbeat_at",
            "last_device_contact_at",
            "last_sync_at",
            "last_error",
            "queued_records",
            "failed_uploads",
            "heartbeat_interval_seconds",
            "sync_interval_seconds",
            "offline_after_seconds",
            "degraded_queue_threshold",
            "settings",
            "notes",
            "devices",
            "device_office_names",
            "office_mismatch",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=choices(health.AGENT_STATUSES)))
    def get_status(self, agent) -> str:
        return health.agent_status(agent, moment(self))

    def get_is_online(self, agent) -> bool:
        return health.agent_is_online(agent, moment(self))

    def get_seconds_since_heartbeat(self, agent) -> int | None:
        return health.seconds_since_heartbeat(agent, moment(self))

    def get_token_prefix(self, agent) -> str | None:
        return agent.credential.token_prefix if agent.credential_id else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_token_issued_at(self, agent):
        return agent.credential.issued_at if agent.credential_id else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_token_revoked_at(self, agent):
        return agent.credential.revoked_at if agent.credential_id else None

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_token_last_used_at(self, agent):
        return agent.credential.last_used_at if agent.credential_id else None

    def get_device_office_names(self, agent) -> list[str]:
        return sorted({device.office.name for device in agent.devices.all() if device.office_id})

    def get_office_mismatch(self, agent) -> bool:
        return any(device.office_id is not None and device.office_id != agent.office_id for device in agent.devices.all())


class AgentCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=60, help_text="Stable identity, e.g. OFFICE-001-AGENT.")
    name = serializers.CharField(max_length=120)
    office = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="Office uid.")
    heartbeat_interval_seconds = serializers.IntegerField(min_value=10, max_value=3600, required=False, default=60)
    sync_interval_seconds = serializers.IntegerField(min_value=30, max_value=86400, required=False, default=300)
    offline_after_seconds = serializers.IntegerField(min_value=10, max_value=86400, required=False, default=300)
    degraded_queue_threshold = serializers.IntegerField(min_value=1, max_value=10_000_000, required=False, default=500)
    settings = serializers.JSONField(required=False, default=dict)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_settings(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be a JSON object.")
        return value


class AgentUpdateSerializer(ExpectedVersionMixin, AgentCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        fields.pop("code")
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields

    def to_representation(self, instance):
        return AgentSerializer(instance, context=self.context).data


class ConfigDownloadSerializer(serializers.Serializer):
    download = serializers.CharField(help_text="Single-use key for GET …/config-download/?download=<key> (agent.ini with the token).")
    expires_at = serializers.DateTimeField()


class AgentTokenSerializer(serializers.Serializer):
    agent = AgentSerializer()
    token = serializers.CharField(help_text="Shown once: only a sha256 is stored. Rotating issues a new token and invalidates this one.")
    config_download = ConfigDownloadSerializer()
    warning = serializers.CharField()


class ConfigDownloadQuerySerializer(serializers.Serializer):
    download = serializers.CharField(max_length=128)
