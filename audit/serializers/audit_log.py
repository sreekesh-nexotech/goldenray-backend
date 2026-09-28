from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from audit.models import AuditLog


class AuditActorSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    email = serializers.EmailField()
    name = serializers.CharField()


class AuditLogSerializer(serializers.ModelSerializer):
    """One audit row. Rows have no external id of their own; they are addressed through their object and time."""

    actor = serializers.SerializerMethodField()
    before = serializers.JSONField(allow_null=True, read_only=True)
    after = serializers.JSONField(allow_null=True, read_only=True)

    class Meta:
        model = AuditLog
        fields = ["at", "action", "actor", "actor_kind", "request_id", "ip", "object_type", "object_uid", "before", "after", "note"]
        read_only_fields = fields

    @extend_schema_field(AuditActorSerializer(allow_null=True))
    def get_actor(self, entry):
        user = entry.actor
        if user is None:
            return None
        return {"uid": user.uid, "email": user.email, "name": user.get_full_name()}
