from rest_framework import serializers


class ErrorSerializer(serializers.Serializer):
    """The error envelope every endpoint returns on failure (documented in OpenAPI)."""

    code = serializers.CharField()
    message = serializers.CharField()
    errors = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))
    error_codes = serializers.ListField(child=serializers.CharField())


class ExpectedVersionMixin(serializers.Serializer):
    """Adds the optimistic-locking token accepted by every PATCH/action on a versioned row."""

    expected_version = serializers.IntegerField(min_value=1, required=False, write_only=True, help_text="The `version` the client last read; 409 `stale_version` when outdated.")
