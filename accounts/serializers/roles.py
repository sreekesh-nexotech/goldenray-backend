"""``roles/`` shapes and the registry description served by ``roles/registry/``."""

from rest_framework import serializers

from accounts.models import Role
from core.serializers.common import ExpectedVersionMixin

PERMISSIONS_HELP = "`{module: [actions]}`; validated strictly against `GET roles/registry/`."
SCOPES_HELP = "`{module: scope}` for permitted modules; omitted modules get their narrowest scope."


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.DictField(child=serializers.ListField(child=serializers.CharField()), read_only=True)
    scopes = serializers.DictField(child=serializers.CharField(), read_only=True)
    user_count = serializers.IntegerField(read_only=True, default=0, help_text="Live users holding this role.")

    class Meta:
        model = Role
        fields = ["uid", "slug", "name", "description", "is_system", "permissions", "scopes", "user_count", "legacy_role", "created_at", "updated_at", "version"]
        read_only_fields = fields


class _RoleWriteSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=64)
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(allow_blank=True, required=False, max_length=2000)
    permissions = serializers.DictField(child=serializers.ListField(child=serializers.CharField(max_length=32), max_length=32), required=False, help_text=PERMISSIONS_HELP)
    scopes = serializers.DictField(child=serializers.CharField(max_length=16), required=False, help_text=SCOPES_HELP)

    def to_representation(self, instance):
        from accounts.services.roles import roles_queryset

        annotated = roles_queryset().filter(pk=instance.pk).first() or instance
        return RoleSerializer(annotated, context=self.context).data


class RoleCreateSerializer(_RoleWriteSerializer):
    pass


class RoleUpdateSerializer(ExpectedVersionMixin, _RoleWriteSerializer):
    slug = serializers.SlugField(max_length=64, required=False, help_text="Fixed for system roles.")
    name = serializers.CharField(max_length=120, required=False)


class RegistryActionSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()


class RegistryModuleSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    actions = serializers.ListField(child=serializers.CharField())
    scopes = serializers.ListField(child=serializers.CharField())
    default_scope = serializers.CharField()
    description = serializers.CharField()
    permission_only = serializers.BooleanField(help_text="Unlocks fields or aggregate views; no endpoint of its own.")


class RegistryGroupSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    modules = RegistryModuleSerializer(many=True)


class RegistrySelfActionSerializer(serializers.Serializer):
    module = serializers.CharField()
    action = serializers.CharField()


class RegistrySerializer(serializers.Serializer):
    actions = RegistryActionSerializer(many=True)
    scopes = RegistryActionSerializer(many=True)
    groups = RegistryGroupSerializer(many=True)
    self_action_denied = RegistrySelfActionSerializer(many=True)
