"""The office-agent protocol shapes (``/api/agent/<version>/``)."""

from __future__ import annotations

from rest_framework import serializers

from devices.models import Device
from devices.serializers.refs import OfficeRefSerializer
from devices.services.common import INT_RANGE

# The agent's own counters land in int columns: beyond them a heartbeat is a 400, never a database error. Terminal data
# (user tables, punches) is never refused for one odd number: the services keep what fits and count the rest invalid.
INT_MAX = INT_RANGE[1]


class AgentIdentitySerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    office = OfficeRefSerializer(allow_null=True)


class IntervalsSerializer(serializers.Serializer):
    heartbeat_seconds = serializers.IntegerField()
    sync_seconds = serializers.IntegerField()


class ConfigDeviceSerializer(serializers.ModelSerializer):
    awaiting_discovery = serializers.SerializerMethodField(help_text="No LAN address yet: find it with `discover`.")

    class Meta:
        model = Device
        fields = [
            "uid",
            "name",
            "serial_number",
            "expected_serial",
            "expected_mac",
            "ip_address",
            "port",
            "protocol",
            "comm_password",
            "timeout_seconds",
            "awaiting_discovery",
            "users_read_requested_at",
        ]
        read_only_fields = fields

    def get_awaiting_discovery(self, device) -> bool:
        return not device.ip_address


class AgentConfigSerializer(serializers.Serializer):
    agent = AgentIdentitySerializer()
    intervals = IntervalsSerializer()
    announce_interval_seconds = serializers.IntegerField(help_text="Announce each terminal once per process start and then at most this often.")
    upload_batch_size = serializers.IntegerField()
    devices = ConfigDeviceSerializer(many=True, help_text="Active terminals bound to this agent (the identity each must report included).")
    settings = serializers.DictField()
    server_time = serializers.DateTimeField()


class DeviceHealthReportSerializer(serializers.Serializer):
    device = serializers.UUIDField(required=False, allow_null=True, default=None, help_text="Device uid (or serial_number).")
    serial_number = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True, default=None)
    reachable = serializers.BooleanField(help_text="The agent reached the terminal on the LAN just now (and it answered as itself).")
    last_error = serializers.CharField(max_length=2000, required=False, allow_blank=True, allow_null=True, default=None)
    device_time = serializers.CharField(
        max_length=40, required=False, allow_blank=True, allow_null=True, default=None, help_text="The terminal's clock (naive, as read) — measures clock_offset_seconds."
    )
    observed_at = serializers.DateTimeField(required=False, allow_null=True, default=None, help_text="When the agent read that clock (UTC).")


class HeartbeatSerializer(serializers.Serializer):
    version = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    hostname = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    platform = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    local_ip = serializers.IPAddressField(required=False, allow_null=True, default=None)
    queued_records = serializers.IntegerField(min_value=0, max_value=INT_MAX, required=False, default=0)
    failed_uploads = serializers.IntegerField(min_value=0, max_value=INT_MAX, required=False, default=0)
    last_error = serializers.CharField(max_length=2000, required=False, allow_blank=True, allow_null=True, default=None)
    devices = DeviceHealthReportSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        attrs["agent_version"] = attrs.pop("version", "")
        return attrs


class AnnounceSerializer(serializers.Serializer):
    serial_number = serializers.CharField(max_length=80, help_text="The serial the terminal reported (identity).")
    name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    ip_address = serializers.IPAddressField(required=False, allow_null=True, default=None)
    port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=4370)
    protocol = serializers.ChoiceField(choices=Device.Protocol.choices, required=False, default=Device.Protocol.ZK_TCP)
    model = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    firmware_version = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    platform = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    mac_address = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default=None)
    device_time = serializers.CharField(max_length=40, required=False, allow_blank=True, allow_null=True, default=None)
    observed_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    device_info = serializers.DictField(required=False, default=dict)


class AnnounceResultSerializer(serializers.Serializer):
    device = serializers.UUIDField(source="uid")
    name = serializers.CharField()
    serial_number = serializers.CharField()
    office = OfficeRefSerializer(allow_null=True)
    created = serializers.BooleanField()


class IdentityMismatchSerializer(serializers.Serializer):
    device = serializers.UUIDField(required=False, allow_null=True, default=None)
    expected_serial = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True, default=None)
    reported_serial = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True, default=None)
    expected_mac = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default=None)
    reported_mac = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default=None)
    ip_address = serializers.IPAddressField(required=False, allow_null=True, default=None)
    port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=4370)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    observed_at = serializers.DateTimeField(required=False, allow_null=True, default=None)


class IdentityMismatchResultSerializer(serializers.Serializer):
    recorded = serializers.BooleanField()
    device = serializers.UUIDField(allow_null=True)
    identity_status = serializers.CharField()
    message = serializers.CharField()


class DiscoveredHostSerializer(serializers.Serializer):
    ip_address = serializers.IPAddressField()
    port = serializers.IntegerField(min_value=1, max_value=65535, required=False, default=4370)
    mac_address = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default=None)
    serial_number = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True, default=None)
    device_name = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True, default=None)
    firmware_version = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True, default=None)
    platform = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True, default=None)
    error = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True, default=None)


class DiscoverySerializer(serializers.Serializer):
    subnet = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True, default=None)
    agent_ip = serializers.IPAddressField(required=False, allow_null=True, default=None)
    gateway = serializers.IPAddressField(required=False, allow_null=True, default=None)
    hosts_scanned = serializers.IntegerField(min_value=0, required=False, default=0)
    hosts_open = serializers.IntegerField(min_value=0, required=False, default=0)
    scanned_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    found = DiscoveredHostSerializer(many=True, required=False, default=list, max_length=4096)


class DiscoveryMatchSerializer(serializers.Serializer):
    device = serializers.UUIDField()
    reported_serial = serializers.CharField()
    ip_address = serializers.CharField(allow_null=True)
    mac_address = serializers.CharField(allow_blank=True)
    matched = serializers.BooleanField()
    identity_status = serializers.CharField()
    message = serializers.CharField()


class DiscoveryResultSerializer(serializers.Serializer):
    recorded = serializers.BooleanField()
    matches = DiscoveryMatchSerializer(many=True)
    unmatched = DiscoveredHostSerializer(many=True)
    still_awaiting = serializers.ListField(child=serializers.CharField())


class TargetSerializer(serializers.Serializer):
    device = serializers.UUIDField(required=False, allow_null=True, default=None, help_text="Device uid (or serial_number).")
    serial_number = serializers.CharField(max_length=80, required=False, allow_blank=True, allow_null=True, default=None)

    def validate(self, attrs):
        if not attrs.get("device") and not (attrs.get("serial_number") or "").strip():
            raise serializers.ValidationError({"device": ["Name the device by uid or serial_number."]})
        return attrs


class AgentUserSerializer(serializers.Serializer):
    pin = serializers.CharField(max_length=200, allow_blank=True, help_text="The terminal's user id, always a string.")
    device_uid = serializers.IntegerField(required=False, allow_null=True, default=None, help_text="The terminal's internal row id (kept only when an int column holds it).")
    name = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True, default="")
    privilege = serializers.IntegerField(required=False, allow_null=True, default=None, help_text="The terminal's privilege byte (kept only when a smallint holds it).")
    card = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True, default="")
    group_id = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True, default="")
    has_password = serializers.BooleanField(required=False, default=False)
    raw_payload = serializers.DictField(required=False, default=dict)


class SyncUsersSerializer(TargetSerializer):
    users = AgentUserSerializer(many=True, max_length=20000, help_text="The terminal's WHOLE user table (it is the presence watermark).")
    read_at = serializers.DateTimeField(required=False, allow_null=True, default=None)


class AgentPunchSerializer(serializers.Serializer):
    device_record_uid = serializers.IntegerField(required=False, allow_null=True, default=None)
    pin = serializers.CharField(max_length=200, allow_blank=True)
    device_time = serializers.CharField(max_length=40, help_text="The terminal's wall clock, naive (YYYY-MM-DDTHH:MM:SS); an offset is dropped, never converted.")
    status = serializers.IntegerField(required=False, allow_null=True, default=None, help_text="Raw code, never interpreted (beyond a smallint the record is counted invalid).")
    punch = serializers.IntegerField(required=False, allow_null=True, default=None, help_text="Raw code, never interpreted (beyond a smallint the record is counted invalid).")
    raw_payload = serializers.DictField(required=False, default=dict)


class SyncAttendanceSerializer(TargetSerializer):
    batch_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    records = AgentPunchSerializer(many=True, min_length=1, max_length=200, help_text="At most 200 punches per batch.")


class UsersResultSerializer(serializers.Serializer):
    device = serializers.UUIDField()
    received = serializers.IntegerField()
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    invalid = serializers.IntegerField()


class AttendanceResultSerializer(serializers.Serializer):
    device = serializers.UUIDField()
    received = serializers.IntegerField()
    new = serializers.IntegerField()
    duplicate = serializers.IntegerField()
    invalid = serializers.IntegerField()
    discarded = serializers.IntegerField(help_text="Accepted but not stored: no punch store is installed yet.")


class SyncStatusQuerySerializer(TargetSerializer):
    pass


class SyncStatusSerializer(serializers.Serializer):
    device = serializers.UUIDField()
    stored_records = serializers.IntegerField(allow_null=True)
    highest_device_record_uid = serializers.IntegerField(allow_null=True)
    latest_device_time = serializers.CharField(allow_null=True, help_text="Newest stored punch, the terminal's wall clock (naive ISO).")
    last_sync_at = serializers.DateTimeField(allow_null=True)
    punch_store_installed = serializers.BooleanField()
    server_time = serializers.DateTimeField()
