"""Project shapes (staff). ``cost_inputs`` and the landed cost of the locked BOM need ``pricing_internal.view``."""

from __future__ import annotations

import copy
from decimal import Decimal

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.models import Customer
from customers.serializers.customers import ActiveUserField, UserRefSerializer
from engines.bom_domain import Role
from pricing.models.choices import InstallationType
from pricing.services.common import can_see_internal
from projects.models import KsebStatus, Phase, Project, SystemType

TIERS = ("BASE", "VALUE", "PREMIUM")
SELECTION_METHODS = ("PACKAGE_DEFAULT", "PROJECT_HEAD_OVERRIDE", "ENGINEER_PROPOSAL", "PROCUREMENT_SUBSTITUTION", "FALLBACK")
INSTALLATION_TYPE_CHOICES = InstallationType.choices  # the pricing installation-matrix roof columns


@extend_schema_serializer(component_name="ProjectCustomerRef")
class CustomerRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


def _internal(serializer) -> bool:
    request = serializer.context.get("request")
    return can_see_internal(getattr(request, "user", None))


class ProjectSerializer(serializers.ModelSerializer):
    customer = CustomerRefSerializer(read_only=True)
    head = UserRefSerializer(read_only=True, allow_null=True)
    bom_locked = serializers.SerializerMethodField()
    bom_locked_by = UserRefSerializer(read_only=True, allow_null=True)
    engineering_run_uid = serializers.UUIDField(source="engineering_run.uid", read_only=True, allow_null=True, default=None)

    class Meta:
        model = Project
        fields = [
            "uid",
            "number",
            "customer",
            "title",
            "status",
            "head",
            "system_type",
            "tier",
            "size_kw",
            "phase",
            "quotation_version_uid",
            "agreement_uid",
            "site_inspection_uid",
            "lead_uid",
            "bom_locked",
            "bom_locked_at",
            "bom_locked_by",
            "engineering_run_uid",
            "scheduled_on",
            "commissioned_on",
            "kseb_status",
            "closed_at",
            "cancelled_at",
            "cancel_reason",
            "note",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_bom_locked(self, obj) -> bool:
        return obj.bom_lock is not None


class ProjectDetailSerializer(ProjectSerializer):
    bom_lock = serializers.SerializerMethodField(help_text="The locked BOM snapshot; `unit_landed_cost` is null without pricing_internal.view.")
    cost_inputs = serializers.SerializerMethodField(help_text="Null without pricing_internal.view.")

    class Meta(ProjectSerializer.Meta):
        fields = [*ProjectSerializer.Meta.fields, "bom_lock", "cost_inputs"]
        read_only_fields = fields

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_bom_lock(self, obj):
        if obj.bom_lock is None or _internal(self):
            return obj.bom_lock
        redacted = copy.deepcopy(obj.bom_lock)
        for line in redacted.get("lines") or []:
            line["unit_landed_cost"] = None
        return redacted

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_cost_inputs(self, obj):
        return obj.cost_inputs if _internal(self) else None


class _ProjectWriteFields(serializers.Serializer):
    customer_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Customer.objects.all(), source="customer", help_text="A live customer.")
    head_uid = ActiveUserField(source="head", required=False, allow_null=True, help_text="The responsible Project Head (active user).")
    title = serializers.CharField(required=False, allow_blank=True, max_length=160)
    system_type = serializers.ChoiceField(choices=SystemType.choices, required=False, allow_blank=True)
    tier = serializers.ChoiceField(choices=[(tier, tier.title()) for tier in TIERS], required=False, allow_blank=True)
    size_kw = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True)
    phase = serializers.ChoiceField(choices=Phase.choices, required=False, allow_blank=True)
    quotation_version_uid = serializers.UUIDField(required=False, allow_null=True)
    agreement_uid = serializers.UUIDField(required=False, allow_null=True)
    site_inspection_uid = serializers.UUIDField(required=False, allow_null=True)
    lead_uid = serializers.UUIDField(required=False, allow_null=True)
    scheduled_on = serializers.DateField(required=False, allow_null=True)
    kseb_status = serializers.ChoiceField(choices=KsebStatus.choices, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=10_000)


class ProjectCreateSerializer(_ProjectWriteFields):
    pass


class ProjectUpdateSerializer(ExpectedVersionMixin, _ProjectWriteFields):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer_uid"].required = False


class BomLineSerializer(serializers.Serializer):
    component_uid = serializers.UUIDField()
    role = serializers.ChoiceField(choices=[(role.value, role.value) for role in Role])
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    selection_method = serializers.ChoiceField(choices=[(m, m) for m in SELECTION_METHODS], required=False, default="PACKAGE_DEFAULT")
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")


class BomAcknowledgementSerializer(serializers.Serializer):
    rule_code = serializers.RegexField(r"^[A-Z0-9-]{1,16}$", help_text="The rule of a WARN finding that requires an acknowledgement (e.g. PBC-D-003).")
    reason = serializers.CharField(max_length=2000)


class LockBomSerializer(ExpectedVersionMixin):
    architecture = serializers.RegexField(r"^[A-Z_]{1,16}$", help_text="Flarize architecture, e.g. DEYE, ENPHASE, GRID_TIED, HYBRID.")
    lines = BomLineSerializer(many=True, allow_empty=False, max_length=200)
    acknowledgements = BomAcknowledgementSerializer(many=True, required=False, default=list, max_length=100)


class SpecialWorkSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=200)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)


class CostInputsSerializer(ExpectedVersionMixin):
    """Flarize ``costInputs`` (distanceKm, vehicleType, installationType, structureMaterialCost, specialWorks, rateEffectiveAt, sizeKey)."""

    distance_km = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, max_value=5000)
    vehicle_type = serializers.CharField(required=False, allow_blank=True, max_length=32, default="")
    installation_type = serializers.ChoiceField(choices=INSTALLATION_TYPE_CHOICES)
    structure_material_cost = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True, default=None)
    special_works = SpecialWorkSerializer(many=True, required=False, default=list, max_length=50)
    rate_effective_at = serializers.DateField(required=False, allow_null=True, default=None)
    size_key = serializers.RegexField(r"^[0-9]{1,3}(sp|tp)?$", required=False, allow_blank=True, default="", help_text="Pack size key, e.g. 3, 5sp, 5tp.")


class CommissionSerializer(ExpectedVersionMixin):
    commissioned_on = serializers.DateField(required=False, help_text="Default today; never in the future.")
    kseb_status = serializers.ChoiceField(choices=KsebStatus.choices, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=10_000, default="")


class CloseSerializer(ExpectedVersionMixin):
    note = serializers.CharField(required=False, allow_blank=True, max_length=10_000, default="")


class ProjectCancelSerializer(ExpectedVersionMixin):
    reason = serializers.CharField(max_length=2000)
