"""``users/`` shapes. Writes return the full ``UserSerializer`` representation."""

from django.core.validators import RegexValidator
from rest_framework import serializers

from accounts.models import Role, User
from core.serializers.common import ExpectedVersionMixin

PHONE_VALIDATOR = RegexValidator(r"^\+[1-9][0-9]{6,14}$", "Use the international E.164 format, e.g. +919876543210.")


class RoleRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["uid", "slug", "name"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role = RoleRefSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            "uid",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone_e164",
            "title",
            "role",
            "is_active",
            "must_reset_password",
            "last_login_at",
            "password_changed_at",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class _UserWriteSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    phone_e164 = serializers.CharField(max_length=16, allow_blank=True, required=False, validators=[PHONE_VALIDATOR])
    title = serializers.CharField(max_length=64, allow_blank=True, required=False)
    role = serializers.SlugRelatedField(slug_field="uid", queryset=Role.objects.all(), help_text="Role uid.")

    def to_representation(self, instance):
        return UserSerializer(instance, context=self.context).data


class UserCreateSerializer(_UserWriteSerializer):
    """Creates the account without a password and e-mails an invitation link."""


class UserUpdateSerializer(ExpectedVersionMixin, _UserWriteSerializer):
    email = serializers.EmailField(max_length=254, required=False, help_text="Needs users.manage.")
    role = serializers.SlugRelatedField(slug_field="uid", queryset=Role.objects.all(), required=False, help_text="Role uid. Needs users.manage.")


class UserActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, help_text="Reason, recorded in the audit log.")
