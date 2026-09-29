"""Employee shapes (staff ``hr/employees/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from hr.models import Employee, Office, Shift
from hr.serializers.refs import EmployeeRefSerializer, HrUserRefSerializer, OfficeRefSerializer, ShiftRefSerializer, SignedPhotoField
from hr.services.employees import employees_queryset


class EmployeeSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)
    shift = ShiftRefSerializer(read_only=True, allow_null=True)
    effective_shift = serializers.SerializerMethodField(help_text="The shift the engine uses: the employee's own, else the office default.")
    user = HrUserRefSerializer(read_only=True, allow_null=True, help_text="The linked Studio login.")
    photo = SignedPhotoField(help_text="Private photo; signed URLs valid for 10 minutes.")

    class Meta:
        model = Employee
        fields = [
            "uid",
            "code",
            "full_name",
            "office",
            "shift",
            "effective_shift",
            "department",
            "designation",
            "email",
            "phone_e164",
            "joined_on",
            "left_on",
            "identity_method",
            "is_active",
            "user",
            "photo",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(ShiftRefSerializer(allow_null=True))
    def get_effective_shift(self, employee):
        shift = employee.effective_shift
        return ShiftRefSerializer(shift).data if shift is not None else None


class EmployeeCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    full_name = serializers.CharField(max_length=150)
    office = serializers.SlugRelatedField(slug_field="uid", queryset=Office.objects.all(), required=False, allow_null=True, default=None, help_text="Office uid.")
    shift = serializers.SlugRelatedField(slug_field="uid", queryset=Shift.objects.all(), required=False, allow_null=True, default=None, help_text="Shift uid (empty: the office default).")
    department = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    designation = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    email = serializers.CharField(max_length=254, required=False, allow_blank=True, default="", help_text="Contact address (not the login).")
    phone_e164 = serializers.CharField(max_length=32, required=False, allow_blank=True, default="", help_text="Stored as E.164; Indian numbers may omit +91.")
    joined_on = serializers.DateField(required=False, allow_null=True, default=None)
    left_on = serializers.DateField(required=False, allow_null=True, default=None)
    identity_method = serializers.ChoiceField(choices=Employee.IdentityMethod.choices, required=False, default=Employee.IdentityMethod.UNSPECIFIED)

    def to_representation(self, instance):
        return EmployeeSerializer(employees_queryset().get(pk=instance.pk), context=self.context).data


class EmployeeUpdateSerializer(ExpectedVersionMixin, EmployeeCreateSerializer):
    def get_fields(self):
        fields = super().get_fields()
        for name, field in fields.items():
            if name != "expected_version":
                field.required = False
                field.default = serializers.empty
        return fields


class EmployeeActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="", help_text="Recorded in the audit log.")


class EmployeeDeactivateSerializer(EmployeeActionSerializer):
    left_on = serializers.DateField(required=False, allow_null=True, default=None, help_text="Optionally record the leaving date.")


class LinkUserSerializer(ExpectedVersionMixin, serializers.Serializer):
    user_uid = serializers.UUIDField(required=False, allow_null=True, default=None, help_text="An existing account.")
    email = serializers.EmailField(required=False, allow_blank=True, default="", help_text="Or: create a Staff account and e-mail an invitation.")
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if bool(attrs.get("user_uid")) == bool(attrs.get("email")):
            raise serializers.ValidationError({"user_uid": ["Send either user_uid (an existing account) or email (a new one)."]})
        return attrs


class LinkResultSerializer(serializers.Serializer):
    employee = EmployeeSerializer()
    staff_role_assigned = serializers.BooleanField(required=False)
    account_created = serializers.BooleanField(required=False)
    account_reactivated = serializers.BooleanField(required=False)
    account_deactivated = serializers.BooleanField(required=False)


class PhotoUploadSerializer(ExpectedVersionMixin, serializers.Serializer):
    file = serializers.FileField(allow_empty_file=True, use_url=False, help_text="JPEG, PNG, WebP or HEIC, at most 15 MB (type detected from the content).")


class DependenciesSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    counts = serializers.DictField(child=serializers.IntegerField(), help_text="leave_records, plus attendance_days, raw_punches, device_mappings once those packages are installed.")
    has_login = serializers.BooleanField()
    user = HrUserRefSerializer(allow_null=True)
    can_delete = serializers.BooleanField(help_text="False when any history exists: deactivate instead.")


class DeviceMappingsSerializer(serializers.Serializer):
    employee = EmployeeRefSerializer()
    available = serializers.BooleanField(help_text="False until the devices package provides mappings.")
    mappings = serializers.ListField(child=serializers.DictField())
    details = serializers.DictField()


class ReconcileRequestSerializer(serializers.Serializer):
    read_devices = serializers.BooleanField(required=False, default=True)
    apply = serializers.BooleanField(required=False, default=False)
    confirm = serializers.BooleanField(required=False, default=False)


class ReconcileResultSerializer(serializers.Serializer):
    result = serializers.DictField(help_text="The devices package's report (added, updated, removed, unchanged, unknown, unlinked, unconfirmed).")
