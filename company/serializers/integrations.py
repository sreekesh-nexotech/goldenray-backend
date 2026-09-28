"""``settings/integrations/`` shapes. Secret values are write-only: responses say only whether each is set."""

from rest_framework import serializers

from company.models import Integration


class IntegrationFieldSerializer(serializers.Serializer):
    name = serializers.CharField()
    type = serializers.CharField()
    required = serializers.BooleanField()
    secret = serializers.BooleanField()
    default = serializers.JSONField(allow_null=True)


class IntegrationSerializer(serializers.Serializer):
    key = serializers.ChoiceField(choices=Integration.Key.choices)
    label = serializers.CharField()
    uid = serializers.UUIDField(allow_null=True)
    is_enabled = serializers.BooleanField()
    config = serializers.DictField(help_text="Non-secret values.")
    secrets = serializers.DictField(child=serializers.BooleanField(), help_text="{secret field: is it set}.")
    fields = IntegrationFieldSerializer(many=True)
    version = serializers.IntegerField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class IntegrationPutSerializer(serializers.Serializer):
    key = serializers.ChoiceField(choices=Integration.Key.choices)
    is_enabled = serializers.BooleanField()
    config = serializers.DictField(help_text="Field values; secrets: omit to keep, a string to replace, null or '' to clear.")
    expected_version = serializers.IntegerField(min_value=1, required=False, write_only=True)
