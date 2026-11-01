"""Change request shapes (staff ``change-requests/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from approvals.models import ChangeRequest
from approvals.services import kinds
from core.serializers import ExpectedVersionMixin


@extend_schema_serializer(component_name="ChangeRequestUserRef")
class UserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField(source="get_full_name")


class ChangeRequestSerializer(serializers.ModelSerializer):
    kind_label = serializers.SerializerMethodField()
    module = serializers.SerializerMethodField()
    requested_by = UserRefSerializer(source="created_by", read_only=True, allow_null=True)
    decided_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = ChangeRequest
        fields = [
            "uid",
            "kind",
            "kind_label",
            "module",
            "operation",
            "target_uid",
            "target_label",
            "payload",
            "before",
            "base_version",
            "reason",
            "status",
            "requested_by",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_note",
            "result_uid",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def get_kind_label(self, request) -> str:
        return kinds.label_of(request.kind)

    def get_module(self, request) -> str:
        try:
            return kinds.get(request.kind).module
        except Exception:  # a kind no longer registered still lists
            return ""


class ChangeRequestCreateSerializer(serializers.Serializer):
    kind = serializers.CharField(max_length=64, help_text="A registered change kind, e.g. `bom.fixed_item`, `pricing.cost_config`.")
    operation = serializers.ChoiceField(choices=ChangeRequest.Operation.choices)
    target_uid = serializers.UUIDField(required=False, allow_null=True, default=None, help_text="The row to update or delete.")
    payload = serializers.DictField(required=False, default=dict, help_text="Exactly the body the direct endpoint takes (`expected_version` pins the row's version).")
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ChangeRequestDecisionSerializer(ExpectedVersionMixin, serializers.Serializer):
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="", help_text="Required when rejecting.")


class ChangeKindSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    module = serializers.CharField()
    operations = serializers.ListField(child=serializers.CharField())
