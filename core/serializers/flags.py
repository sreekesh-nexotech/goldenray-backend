from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin


class FeatureFlagSerializer(serializers.Serializer):
    key = serializers.CharField()
    enabled = serializers.BooleanField()
    default = serializers.BooleanField(help_text="Value when no override row exists.")
    note = serializers.CharField(allow_blank=True)
    uid = serializers.UUIDField(allow_null=True, help_text="Override row uid; null while the default is in force.")
    version = serializers.IntegerField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)

    @staticmethod
    def from_state(state: dict) -> dict:
        row = state["row"]
        return {
            "key": state["key"],
            "enabled": state["enabled"],
            "default": state["default"],
            "note": row.note if row else "",
            "uid": row.uid if row else None,
            "version": row.version if row else None,
            "updated_at": row.updated_at if row else None,
        }


class FeatureFlagUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    key = serializers.CharField(max_length=64)
    enabled = serializers.BooleanField()
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)
