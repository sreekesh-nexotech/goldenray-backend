"""Compact embedded references used across the hr responses."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import User
from accounts.serializers.users import RoleRefSerializer
from hr.models import Employee, LeaveType, Office, Shift
from media.services import signing


class OfficeRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Office
        fields = ["uid", "code", "name", "timezone"]
        read_only_fields = fields


class ShiftRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ["uid", "code", "name", "start_time", "end_time", "is_overnight"]
        read_only_fields = fields


class EmployeeRefSerializer(serializers.ModelSerializer):
    office = OfficeRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Employee
        fields = ["uid", "code", "full_name", "office", "is_active"]
        read_only_fields = fields


class LeaveTypeRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ["uid", "code", "name", "paid"]
        read_only_fields = fields


class HrUserRefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role = RoleRefSerializer(read_only=True)

    class Meta:
        model = User
        fields = ["uid", "email", "full_name", "is_active", "role"]
        read_only_fields = fields


class HrActorRefSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["uid", "full_name"]
        read_only_fields = fields


class EmployeePhotoSerializer(serializers.Serializer):
    """A private photo: short-lived signed URLs (10 minutes), never a storage key."""

    uid = serializers.UUIDField()
    url = serializers.CharField()
    thumbnail_url = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


@extend_schema_field(EmployeePhotoSerializer(allow_null=True))
class SignedPhotoField(serializers.Field):
    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return instance.photo if instance.photo_id else None

    def to_representation(self, asset):
        request = self.context.get("request")
        version = getattr(request, "version", None) or "v1"
        signed = signing.signed_url(asset, version=version, absolute=request.build_absolute_uri if request is not None else None)
        return {"uid": str(asset.uid), "url": signed.url, "thumbnail_url": signed.thumbnail_url, "expires_at": signed.expires_at}
