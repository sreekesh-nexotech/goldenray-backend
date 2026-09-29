"""Device shapes (staff ``devices/``): the validated create/update bodies, actions and the derived health."""

from __future__ import annotations

from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from devices.models import Agent, Device
from devices.serializers.refs import AgentRefSerializer, DeviceHealthSerializer, DeviceRowSerializer, OfficeRefSerializer, choices
from devices.services import health, roster
from devices.services.devices import devices_queryset
from hr.models import Office


class DeviceSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)
    agent = AgentRefSerializer(read_only=True, allow_null=True)
    health = DeviceHealthSerializer(source="*", read_only=True)
    has_comm_password = serializers.SerializerMethodField(help_text="The communication key itself is never returned.")
    adms_token_set = serializers.SerializerMethodField(help_text="A /iclock/ path token has been issued (the token is shown once, at enable).")

    class Meta:
        model = Device
        fields = [
            "uid",
            "name",
            "serial_number",
            "expected_serial",
            "expected_mac",
            "mac_address",
            "office",
            "agent",
            "ip_address",
            "port",
            "protocol",
            "has_comm_password",
            "timeout_seconds",
            "model",
            "firmware_version",
            "platform",
            "adms_enabled",
            "adms_token_set",
            "adms_allowed_ips",
            "adms_registration_state",
            "adms_last_seen_at",
            "adms_last_handshake_at",
            "adms_last_push_at",
            "adms_last_command_poll_at",
            "adms_source_ip",
            "adms_request_count",
            "adms_server",
            "adms_port",
            "identity_status",
            "identity_message",
            "identity_checked_at",
            "last_seen_at",
            "last_sync_at",
            "last_punch_at",
            "last_error",
            "clock_offset_seconds",
            "user_count",
            "attendance_count",
            "device_info",
            "users_read_requested_at",
            "notes",
            "is_active",
            "health",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def get_has_comm_password(self, device) -> bool:
        return bool(device.comm_password)

    def get_adms_token_set(self, device) -> bool:
        return bool(device.adms_token_hash)


class DeviceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    serial_number = serializers.CharField(max_length=80, required=False, allow_null=True, allow_blank=True, default=None, help_text="The serial on the label; becomes the expected serial (pin).")
    expected_serial = serializers.CharField(max_length=80, required=False, allow_null=True, allow_blank=True, default=None)
    expected_mac = serializers.CharField(max_length=32, required=False, allow_blank=True, default="", help_text="Any separator; stored as aa:bb:cc:dd:ee:ff.")
    office = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="Office uid.")
    agent = serializers.SlugRelatedField(slug_field="uid", queryset=Agent.objects.all(), required=False, allow_null=True, default=None, help_text="Agent uid (later changes: rehome/).")
    ip_address = serializers.IPAddressField(required=False, allow_null=True, default=None)
    port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=4370)
    protocol = serializers.ChoiceField(choices=Device.Protocol.choices, required=False, default=Device.Protocol.ZK_TCP)
    comm_password = serializers.IntegerField(min_value=0, max_value=999999, required=False, default=0, write_only=True)
    timeout_seconds = serializers.IntegerField(min_value=1, max_value=120, required=False, default=15)
    adms_server = serializers.CharField(max_length=120, required=False, allow_blank=True, default="", help_text="What the terminal's screen shows as its push server (observation).")
    adms_port = serializers.IntegerField(min_value=1, max_value=65535, required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)

    def to_representation(self, instance):
        return DeviceSerializer(devices_queryset().get(pk=instance.pk), context=self.context).data


class DeviceUpdateSerializer(ExpectedVersionMixin, DeviceCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        for name in ("serial_number", "agent"):
            fields.pop(name)
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields


class RehomeSerializer(ExpectedVersionMixin, serializers.Serializer):
    agent_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Agent.objects.all(), allow_null=True, help_text="The agent that carries the device from now on (null: none).")
    office_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, help_text="Move the device to this office as well (omit: unchanged).")
    reason = serializers.CharField(max_length=500, help_text="Recorded in the audit log.")


class AdmsEnableSerializer(ExpectedVersionMixin, serializers.Serializer):
    allowed_ips = serializers.ListField(child=serializers.CharField(max_length=64), required=False, help_text="IP addresses or CIDR networks the terminal may push from (omit: unchanged; []: any).")


class AdmsTokenSerializer(serializers.Serializer):
    device = DeviceSerializer()
    token = serializers.CharField(help_text="Shown once. The terminal's push server path is /iclock/<token>/ (only its sha256 is stored).")
    iclock_path = serializers.CharField()
    warning = serializers.CharField()


class ActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass


class RefreshSerializer(serializers.Serializer):
    requested = serializers.BooleanField(help_text="A read was requested from the device's agent.")
    requested_at = serializers.DateTimeField(allow_null=True)
    transport = serializers.ChoiceField(choices=choices(health.TRANSPORTS))
    note = serializers.CharField()


class EmployeeBriefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    code = serializers.CharField()
    full_name = serializers.CharField()
    is_active = serializers.BooleanField()


class CategoryRowSerializer(serializers.Serializer):
    device_user_uid = serializers.UUIDField()
    pin = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    category = serializers.ChoiceField(choices=choices(roster.CATEGORIES))
    device_state = serializers.ChoiceField(choices=choices(roster.DEVICE_STATES))
    software_state = serializers.ChoiceField(choices=choices(roster.SOFTWARE_STATES))
    is_active_user = serializers.BooleanField()
    employee = EmployeeBriefSerializer(allow_null=True)
    suggested_employee = EmployeeBriefSerializer(allow_null=True, help_text="Only a proposal: nothing acts on it.")
    candidate_employees = EmployeeBriefSerializer(many=True, help_text="Name matches (at most 5); more than one = ambiguous, never auto-linked.")
    ambiguous = serializers.BooleanField()
    available_action = serializers.ChoiceField(choices=choices(roster.ACTIONS))
    requires_confirmation = serializers.BooleanField()
    first_seen_at = serializers.DateTimeField(allow_null=True)
    last_seen_on_device_at = serializers.DateTimeField(allow_null=True)
    status_note = serializers.CharField()


class CategorySummarySerializer(serializers.Serializer):
    new = serializers.IntegerField()
    matched = serializers.IntegerField()
    missing = serializers.IntegerField()
    unknown = serializers.IntegerField()
    total = serializers.IntegerField()


class RemovalSerializer(serializers.Serializer):
    device_user_uid = serializers.UUIDField()
    pin = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    employee = EmployeeBriefSerializer(allow_null=True)


class UserReconciliationSerializer(serializers.Serializer):
    users_last_confirmed_at = serializers.DateTimeField(allow_null=True, help_text="The last successful whole-table read (the presence watermark).")
    sync_state = serializers.ChoiceField(choices=choices(roster.SYNC_STATES))
    sync_error = serializers.CharField(allow_blank=True)
    device_is_active = serializers.BooleanField()
    total_mappings = serializers.IntegerField()
    active_on_device = serializers.IntegerField()
    missing_from_device = serializers.IntegerField()
    pending_sync = serializers.IntegerField()
    sync_failed = serializers.IntegerField()
    device_inactive = serializers.IntegerField()
    deactivated_in_software = serializers.IntegerField()
    unlinked = serializers.IntegerField()
    pending_device_removals = RemovalSerializer(many=True)
    device_deletion_supported = serializers.BooleanField()
    device_deletion_note = serializers.CharField()


class EmployeeReconciliationSerializer(serializers.Serializer):
    device = DeviceRowSerializer()
    transport = serializers.ChoiceField(choices=choices(health.TRANSPORTS))
    refresh = RefreshSerializer(allow_null=True, help_text="Null when nothing was requested (employee-reconciliation/).")
    summary = CategorySummarySerializer()
    rows = CategoryRowSerializer(many=True)
    reconciliation = UserReconciliationSerializer()


class MappingOfficeSerializer(serializers.Serializer):
    office = OfficeRefSerializer()
    devices = serializers.ListField(child=serializers.CharField())
    agents = serializers.ListField(child=serializers.CharField())


class MappingAgentSerializer(serializers.Serializer):
    agent = AgentRefSerializer()
    status = serializers.ChoiceField(choices=choices(health.AGENT_STATUSES))
    serves_devices = serializers.ListField(child=serializers.CharField())
    serves_offices = serializers.ListField(child=serializers.CharField())


class MappingReportSerializer(serializers.Serializer):
    devices = DeviceRowSerializer(many=True)
    offices = MappingOfficeSerializer(many=True)
    agents = MappingAgentSerializer(many=True)
    inconsistencies = DeviceRowSerializer(many=True, help_text="Devices whose agent is filed under another office.")
    truncated = serializers.BooleanField()
