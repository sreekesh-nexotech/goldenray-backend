"""Device-user shapes (staff ``devices/device-users/``): one terminal user per ``(device, PIN)`` (A1)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from devices.models import Device, DeviceUser
from devices.serializers.refs import DeviceRefSerializer, EmployeeRefSerializer, choices
from devices.services import roster
from hr.models import Employee, Office, Shift


class DeviceUserSerializer(serializers.ModelSerializer):
    device = DeviceRefSerializer(read_only=True)
    employee = EmployeeRefSerializer(read_only=True, allow_null=True)
    device_state = serializers.SerializerMethodField(help_text="Still enrolled on the terminal, as of its last successful whole-table read.")
    software_state = serializers.SerializerMethodField()
    sync_state = serializers.SerializerMethodField()
    sync_error = serializers.SerializerMethodField()
    device_confirmed_at = serializers.SerializerMethodField()
    device_active = serializers.SerializerMethodField()
    is_active_user = serializers.SerializerMethodField()
    needs_device_removal = serializers.SerializerMethodField(help_text="Deactivated here, still enrolled there (the platform cannot remove enrolments).")

    class Meta:
        model = DeviceUser
        fields = [
            "uid",
            "device",
            "pin",
            "device_uid",
            "name",
            "privilege",
            "card",
            "group_id",
            "has_password",
            "employee",
            "raw_payload",
            "first_seen_at",
            "last_seen_at",
            "device_state",
            "software_state",
            "sync_state",
            "sync_error",
            "device_confirmed_at",
            "device_active",
            "is_active_user",
            "needs_device_removal",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def _state(self, row) -> dict:
        states = self.context.setdefault("_device_user_states", {})
        if row.pk not in states:
            rows = self.parent.instance if isinstance(self.parent, serializers.ListSerializer) else None  # a page: one batch
            batch = [item for item in rows if isinstance(item, DeviceUser)] if rows is not None else [row]
            if row not in batch:
                batch.append(row)
            states.update(roster.describe(batch))
        return states[row.pk]

    @extend_schema_field(serializers.ChoiceField(choices=choices(roster.DEVICE_STATES)))
    def get_device_state(self, row) -> str:
        return self._state(row)["device_state"]

    @extend_schema_field(serializers.ChoiceField(choices=choices(roster.SOFTWARE_STATES)))
    def get_software_state(self, row) -> str:
        return self._state(row)["software_state"]

    @extend_schema_field(serializers.ChoiceField(choices=choices(roster.SYNC_STATES)))
    def get_sync_state(self, row) -> str:
        return self._state(row)["sync_state"]

    def get_sync_error(self, row) -> str:
        return self._state(row)["sync_error"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_device_confirmed_at(self, row) -> str | None:
        value = self._state(row)["device_confirmed_at"]
        return serializers.DateTimeField().to_representation(value) if value else None

    def get_device_active(self, row) -> bool:
        return self._state(row)["device_active"]

    def get_is_active_user(self, row) -> bool:
        return self._state(row)["is_active_user"]

    def get_needs_device_removal(self, row) -> bool:
        return self._state(row)["needs_device_removal"]


class DeviceUserQuerySerializer(serializers.Serializer):
    device = serializers.UUIDField(required=False, help_text="Device uid.")
    linked = serializers.BooleanField(required=False, allow_null=True, default=None)
    device_state = serializers.ChoiceField(choices=choices(roster.DEVICE_STATES), required=False)
    software_state = serializers.ChoiceField(choices=choices(roster.SOFTWARE_STATES), required=False)
    active_only = serializers.BooleanField(required=False, default=False, help_text="Only users still enrolled and not deactivated here.")


class LinkSerializer(ExpectedVersionMixin, serializers.Serializer):
    employee_uid = serializers.SlugRelatedField(
        slug_field="uid", queryset=Employee.objects.all(), allow_null=True, help_text="The employee this terminal user is (null: unlink). This device row only."
    )


class AutoLinkQuerySerializer(serializers.Serializer):
    device = serializers.SlugRelatedField(slug_field="uid", queryset=Device.objects.all(), required=False, allow_null=True, default=None, help_text="Limit to one device (uid).")


class AutoLinkResultSerializer(serializers.Serializer):
    linked = serializers.IntegerField()
    still_unlinked = serializers.IntegerField()
    unlinked_pins = serializers.ListField(child=serializers.CharField())


class ResolveSerializer(ExpectedVersionMixin, serializers.Serializer):
    action = serializers.ChoiceField(choices=[(roster.ACTION_LINK_EXISTING, roster.ACTION_LINK_EXISTING), (roster.ACTION_CREATE_EMPLOYEE, roster.ACTION_CREATE_EMPLOYEE)])
    confirm = serializers.BooleanField(required=False, default=False, help_text="Must be true: nothing is created or linked without it.")
    employee_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Employee.objects.all(), required=False, allow_null=True, default=None, help_text="LINK_EXISTING: the employee.")
    employee_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="", help_text="CREATE_EMPLOYEE: defaults to the PIN.")
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="", help_text="CREATE_EMPLOYEE: defaults to the terminal's name.")
    office_uid = serializers.SlugRelatedField(
        slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="CREATE_EMPLOYEE: defaults to the device's office."
    )
    shift_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Shift.objects.all(), required=False, allow_null=True, default=None)


class MapPinSerializer(serializers.Serializer):
    device_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Device.objects.all(), help_text="The terminal the PIN belongs to (a PIN means nothing without its device).")
    pin = serializers.CharField(max_length=80)
    employee_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Employee.objects.all())


class MapPinResultSerializer(serializers.Serializer):
    device_user = DeviceUserSerializer()
    created = serializers.BooleanField(help_text="The row did not exist (the PIN was only ever seen in punches).")
    note = serializers.CharField()


class UnmappedQuerySerializer(serializers.Serializer):
    device = serializers.SlugRelatedField(slug_field="uid", queryset=Device.objects.all(), help_text="Device uid (PINs are per device).")


class SuggestedEmployeeSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    code = serializers.CharField()
    full_name = serializers.CharField()
    is_active = serializers.BooleanField()


class UnmappedSerializer(serializers.Serializer):
    pin = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    punch_count = serializers.IntegerField()
    first_punch_at = serializers.DateTimeField(allow_null=True)
    last_punch_at = serializers.DateTimeField(allow_null=True)
    has_device_user_row = serializers.BooleanField()
    device_user_uid = serializers.UUIDField(allow_null=True)
    suggested_employee = SuggestedEmployeeSerializer(allow_null=True)
