"""Shapes of ``pricing/cost-config/``, ``pricing/installation-matrix/``, ``pricing/statutory-fees/`` and
``pricing/validity-policy/``."""

from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from pricing.models import CostConfig, InstallationMatrix, StatutoryFee, ValidityPolicy
from pricing.services import cost_config

# ── cost config ────────────────────────────────────────────────────────────────────────────────────────────────────


class CostConfigKeySerializer(serializers.Serializer):
    key = serializers.CharField()
    group = serializers.CharField()
    kind = serializers.CharField(help_text="money, integer, fraction or document.")
    description = serializers.CharField()
    internal = serializers.BooleanField(help_text="Margin data: the value is hidden without pricing_internal.view.")


class CostConfigRowSerializer(serializers.ModelSerializer):
    value = serializers.SerializerMethodField()
    hidden = serializers.SerializerMethodField(help_text="True when the value is margin data you may not see (value is null).")

    class Meta:
        model = CostConfig
        fields = ["uid", "key", "value", "hidden", "effective_from", "effective_to", "note", "source_ref", "created_at", "version"]
        read_only_fields = fields

    def _shown(self, row):
        return cost_config.redact(row.key, row.value, internal=self.context.get("internal", False))

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_value(self, row):
        return self._shown(row)

    def get_hidden(self, row) -> bool:
        return self._shown(row) is None and row.value is not None


class CostConfigSerializer(serializers.Serializer):
    keys = CostConfigKeySerializer(many=True)
    current = CostConfigRowSerializer(many=True)


class CostConfigEntrySerializer(serializers.Serializer):
    key = serializers.ChoiceField(choices=sorted(cost_config.KEYS))
    value = serializers.JSONField()
    current_uid = serializers.UUIDField(required=False, allow_null=True, help_text="uid of the current row the client saw (null: none); 409 `stale_version` when it changed.")


class CostConfigPutSerializer(serializers.Serializer):
    entries = CostConfigEntrySerializer(many=True, allow_empty=False)
    effective_from = serializers.DateField(required=False, help_text="Default today.")
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class CostConfigPutResultSerializer(serializers.Serializer):
    written = CostConfigRowSerializer(many=True, help_text="The rows appended (unchanged values write nothing).")
    current = CostConfigRowSerializer(many=True)


# ── installation matrix ────────────────────────────────────────────────────────────────────────────────────────────


class InstallationMatrixSerializer(serializers.ModelSerializer):
    size_key = serializers.CharField(read_only=True, help_text="Flarize matrix key: 3, 5sp, 5tp …")

    class Meta:
        model = InstallationMatrix
        fields = ["uid", "size_kw", "phase", "size_key", "installation_type", "install_cost", "labour_days", "created_at", "updated_at", "version"]
        read_only_fields = fields


class InstallationMatrixWriteSerializer(serializers.Serializer):
    size_kw = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"))
    phase = serializers.ChoiceField(choices=InstallationMatrix._meta.get_field("phase").choices, required=False, allow_blank=True, default="")
    installation_type = serializers.ChoiceField(choices=InstallationMatrix._meta.get_field("installation_type").choices, required=False)
    install_cost = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    labour_days = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=0, required=False, allow_null=True)

    def to_representation(self, instance):
        return InstallationMatrixSerializer(instance, context=self.context).data


class InstallationMatrixUpdateSerializer(ExpectedVersionMixin, InstallationMatrixWriteSerializer):
    size_kw = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"), required=False)
    phase = serializers.ChoiceField(choices=InstallationMatrix._meta.get_field("phase").choices, required=False, allow_blank=True)
    install_cost = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False)


# ── statutory fees ─────────────────────────────────────────────────────────────────────────────────────────────────


class StatutoryFeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = StatutoryFee
        fields = ["uid", "kind", "label", "phase", "capacity_kw_max", "amount", "effective_from", "effective_to", "created_at", "updated_at", "version"]
        read_only_fields = fields


class StatutoryFeeWriteSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=StatutoryFee._meta.get_field("kind").choices)
    label = serializers.CharField(max_length=120)
    phase = serializers.ChoiceField(choices=[("1P", "Single phase"), ("3P", "Three phase")], required=False, allow_null=True)
    capacity_kw_max = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    effective_from = serializers.DateField(required=False)
    effective_to = serializers.DateField(required=False, allow_null=True)

    def to_representation(self, instance):
        return StatutoryFeeSerializer(instance, context=self.context).data


class StatutoryFeeUpdateSerializer(ExpectedVersionMixin, StatutoryFeeWriteSerializer):
    kind = serializers.ChoiceField(choices=StatutoryFee._meta.get_field("kind").choices, required=False)
    label = serializers.CharField(max_length=120, required=False)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)


# ── validity policy ────────────────────────────────────────────────────────────────────────────────────────────────


class ValidityWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValidityPolicy
        fields = ["uid", "policy_id", "days", "grace_days", "effective_from", "effective_to", "status", "version_label", "note"]
        read_only_fields = fields


class ResolvedValiditySerializer(serializers.Serializer):
    days = serializers.IntegerField()
    grace_days = serializers.IntegerField()
    source = serializers.CharField(help_text="WINDOW or DEFAULT.")
    policy_id = serializers.CharField(allow_blank=True)
    version = serializers.CharField(allow_blank=True)


class ValidityPolicySerializer(serializers.Serializer):
    key = serializers.CharField()
    days = serializers.IntegerField(allow_null=True)
    grace_days = serializers.IntegerField(allow_null=True)
    effective_from = serializers.DateTimeField(allow_null=True)
    version_label = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)
    version = serializers.IntegerField(allow_null=True, help_text="Send as expected_version on PUT.")
    windows = ValidityWindowSerializer(many=True)
    resolved_now = ResolvedValiditySerializer(allow_null=True, help_text="What a quotation issued now would get.")


class ValidityWindowWriteSerializer(serializers.Serializer):
    policy_id = serializers.CharField(max_length=64)
    days = serializers.IntegerField(min_value=1, max_value=365)
    grace_days = serializers.IntegerField(min_value=0, max_value=365, required=False, default=0)
    effective_from = serializers.DateTimeField()
    effective_to = serializers.DateTimeField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=ValidityPolicy._meta.get_field("status").choices, required=False)
    version_label = serializers.CharField(max_length=80, required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class ValidityPolicyPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=365, required=False)
    grace_days = serializers.IntegerField(min_value=0, max_value=365, required=False)
    effective_from = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True)
    windows = ValidityWindowWriteSerializer(many=True, required=False, help_text="When sent, replaces the windows (upsert by policy_id; left-out windows become INACTIVE).")
