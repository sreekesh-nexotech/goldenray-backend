"""Serializers of ``engineering/rule-sets/``, ``engineering/runs/`` and ``engineering/findings/<uid>/acknowledge/``."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from engineering.models import Acknowledgement, Engine, Finding, RuleSet, Run, RunResult, Severity, SubjectType


class RuleSetSerializer(serializers.ModelSerializer):
    engine = serializers.ChoiceField(choices=Engine.choices, read_only=True)
    rule_count = serializers.SerializerMethodField()

    class Meta:
        model = RuleSet
        fields = ["uid", "rules_version", "engine", "active", "activated_at", "note", "rule_count", "version", "created_at"]
        read_only_fields = fields

    def get_rule_count(self, obj) -> int:
        return len((obj.rules or {}).get("rules") or [])


class RuleSetDetailSerializer(RuleSetSerializer):
    rules = serializers.JSONField(read_only=True)

    class Meta(RuleSetSerializer.Meta):
        fields = [*RuleSetSerializer.Meta.fields, "rules"]
        read_only_fields = fields


class RuleSetActivateSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1)


class AcknowledgementSerializer(serializers.ModelSerializer):
    acknowledged_by = serializers.SerializerMethodField()

    class Meta:
        model = Acknowledgement
        fields = ["uid", "acknowledged_by", "reason", "at"]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_acknowledged_by(self, obj):
        user = obj.acknowledged_by
        return None if user is None else {"uid": str(user.uid), "email": user.email}


class FindingSerializer(serializers.ModelSerializer):
    severity = serializers.ChoiceField(choices=Severity.choices, read_only=True)
    context = serializers.JSONField(read_only=True)
    acknowledgement = serializers.SerializerMethodField()

    class Meta:
        model = Finding
        fields = ["uid", "rule_code", "severity", "message", "context", "identity", "acknowledgement", "version"]
        read_only_fields = fields

    @extend_schema_field(AcknowledgementSerializer(allow_null=True))
    def get_acknowledgement(self, obj):
        acks = [ack for ack in obj.acknowledgements.all() if ack.deleted_at is None]
        return AcknowledgementSerializer(acks[0]).data if acks else None


class RunSerializer(serializers.ModelSerializer):
    subject_type = serializers.ChoiceField(choices=SubjectType.choices, read_only=True)
    result = serializers.ChoiceField(choices=RunResult.choices, read_only=True)
    rules_version = serializers.CharField(source="rule_set.rules_version", read_only=True)
    summary = serializers.JSONField(read_only=True)

    class Meta:
        model = Run
        fields = ["uid", "subject_type", "subject_uid", "subject_label", "result", "rules_version", "summary", "created_at"]
        read_only_fields = fields


class RunDetailSerializer(RunSerializer):
    findings = serializers.SerializerMethodField()

    class Meta(RunSerializer.Meta):
        fields = [*RunSerializer.Meta.fields, "findings"]
        read_only_fields = fields

    @extend_schema_field(FindingSerializer(many=True))
    def get_findings(self, obj):
        rows = Finding.objects.filter(run=obj).prefetch_related("acknowledgements__acknowledged_by").order_by("sort_order", "id")
        return FindingSerializer(rows, many=True).data


class AcknowledgeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)
    expected_version = serializers.IntegerField(required=False, min_value=1)
