from rest_framework import serializers


class DashboardSerializer(serializers.Serializer):
    modules = serializers.DictField(child=serializers.DictField(child=serializers.IntegerField()), help_text="{module: {counter: value}} for every module the user can view.")
