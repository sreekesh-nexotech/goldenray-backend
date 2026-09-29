"""Shapes of ``procurement/suppliers/``, ``procurement/batches/`` (+ lines, charges, allocation) and
``procurement/price-master/``. Landed-cost fields need ``pricing_internal.view`` (``context["internal"]``)."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog.models import Component
from core.serializers.common import ExpectedVersionMixin
from pricing.serializers.common import INTERNAL_NOTE, InternalFieldsMixin, PricingComponentRefSerializer, PricingSupplierRefSerializer
from procurement.models import Batch, BatchCharge, BatchLine, ChargeKind, Supplier

# ── suppliers ──────────────────────────────────────────────────────────────────────────────────────────────────────


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["uid", "code", "name", "gstin", "contact", "address", "reference", "is_active", "created_at", "updated_at", "version"]
        read_only_fields = fields


class SupplierWriteSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$", max_length=32)
    name = serializers.CharField(max_length=200)
    gstin = serializers.RegexField(r"^[0-9]{2}[A-Za-z0-9]{10}[0-9A-Za-z]{3}$", required=False, allow_blank=True, help_text="15-character GSTIN.")
    contact = serializers.DictField(child=serializers.CharField(max_length=200, allow_blank=True), required=False)
    address = serializers.CharField(required=False, allow_blank=True)
    reference = serializers.CharField(max_length=64, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)

    def to_representation(self, instance):
        return SupplierSerializer(instance, context=self.context).data


class SupplierUpdateSerializer(ExpectedVersionMixin, SupplierWriteSerializer):
    code = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$", max_length=32, required=False)
    name = serializers.CharField(max_length=200, required=False)


# ── batches ────────────────────────────────────────────────────────────────────────────────────────────────────────


class BatchRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    number = serializers.CharField()


class BatchLineSerializer(InternalFieldsMixin, serializers.ModelSerializer):
    internal_fields = ("landed_unit_cost", "allocated_charges", "allocation_pct")
    component = PricingComponentRefSerializer(read_only=True)
    landed_unit_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text=INTERNAL_NOTE)
    allocated_charges = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True, allow_null=True, help_text=INTERNAL_NOTE)
    allocation_pct = serializers.DecimalField(max_digits=9, decimal_places=4, read_only=True, allow_null=True, help_text=INTERNAL_NOTE)
    line_value = serializers.DecimalField(max_digits=27, decimal_places=2, read_only=True)
    purchase_price_uid = serializers.SlugRelatedField(source="price_row", slug_field="uid", read_only=True, allow_null=True)

    class Meta:
        model = BatchLine
        fields = ["uid", "component", "qty", "unit_purchase_price", "line_value", "landed_unit_cost", "allocated_charges", "allocation_pct", "purchase_price_uid"]
        read_only_fields = fields


class BatchChargeSerializer(serializers.ModelSerializer):
    class Meta:
        model = BatchCharge
        fields = ["uid", "kind", "amount", "note"]
        read_only_fields = fields


class BatchSerializer(serializers.ModelSerializer):
    supplier = PricingSupplierRefSerializer(read_only=True)
    reverses = BatchRefSerializer(read_only=True, allow_null=True)
    reversed_by = serializers.SerializerMethodField()

    class Meta:
        model = Batch
        fields = [
            "uid",
            "number",
            "supplier",
            "invoice_no",
            "invoice_date",
            "status",
            "subtotal",
            "charges_total",
            "total",
            "allocation_method",
            "note",
            "effective_from",
            "commit_reason",
            "committed_at",
            "reverses",
            "reversed_by",
            "is_seed",
            "other_charges_declared",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(BatchRefSerializer(allow_null=True))
    def get_reversed_by(self, batch):
        reversal = next(iter(batch.reversals.all()), None)
        return {"uid": reversal.uid, "number": reversal.number} if reversal else None


class BatchDetailSerializer(BatchSerializer):
    lines = BatchLineSerializer(many=True, read_only=True)
    charges = BatchChargeSerializer(many=True, read_only=True)

    class Meta(BatchSerializer.Meta):
        fields = [*BatchSerializer.Meta.fields, "lines", "charges"]
        read_only_fields = fields


class BatchWriteSerializer(serializers.Serializer):
    supplier_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Supplier.objects.all(), source="supplier")
    invoice_no = serializers.CharField(max_length=64, required=False, allow_blank=True, help_text="Supplier invoice / PO number (required to commit).")
    invoice_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True)
    other_charges_declared = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True, help_text="Control total only (must be empty or 0 to commit).")

    def to_representation(self, instance):
        return BatchDetailSerializer(instance, context=self.context).data


class BatchUpdateSerializer(ExpectedVersionMixin, BatchWriteSerializer):
    supplier_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Supplier.objects.all(), source="supplier", required=False)


class BatchLineInputSerializer(serializers.Serializer):
    component_uid = serializers.SlugRelatedField(slug_field="uid", queryset=Component.all_objects.all(), source="component")
    qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_purchase_price = serializers.DecimalField(max_digits=14, decimal_places=2)


class BatchLinesPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    lines = BatchLineInputSerializer(many=True)


class BatchChargeInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=ChargeKind.choices)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    note = serializers.CharField(required=False, allow_blank=True)


class BatchChargesPutSerializer(ExpectedVersionMixin, serializers.Serializer):
    charges = BatchChargeInputSerializer(many=True)


class CommitSerializer(ExpectedVersionMixin, serializers.Serializer):
    reason = serializers.CharField(max_length=2000, help_text="Why the prices change (recorded on every price version).")
    effective_from = serializers.DateField(required=False, help_text="Default today; never in the future.")


class AllocationLineSerializer(InternalFieldsMixin, serializers.Serializer):
    # The same fields BatchLineSerializer hides: the per-line allocation gives the landed cost away
    # (landed = unit price + allocated charges / qty).
    internal_fields = ("allocation_pct", "allocated_charges", "landed_unit_cost", "landed_unit_cost_exact", "formula", "differs_from_purchase_price")
    component = PricingComponentRefSerializer()
    qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_purchase_price = serializers.DecimalField(max_digits=14, decimal_places=2)
    purchase_value = serializers.DecimalField(max_digits=20, decimal_places=2)
    allocation_pct = serializers.DecimalField(max_digits=9, decimal_places=4, required=False, help_text=INTERNAL_NOTE)
    allocated_charges = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, help_text=INTERNAL_NOTE)
    landed_unit_cost = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, help_text=INTERNAL_NOTE)
    landed_unit_cost_exact = serializers.DecimalField(max_digits=24, decimal_places=6, required=False, help_text=INTERNAL_NOTE)
    formula = serializers.CharField(required=False, help_text=INTERNAL_NOTE)
    differs_from_purchase_price = serializers.BooleanField(required=False, help_text=INTERNAL_NOTE)


class BlockerSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class AllocationPreviewSerializer(serializers.Serializer):
    batch = BatchRefSerializer()
    ok = serializers.BooleanField(help_text="The allocation engine accepted the batch.")
    can_commit = serializers.BooleanField()
    blockers = BlockerSerializer(many=True)
    engine_errors = BlockerSerializer(many=True)
    charges_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    allocated_total = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    total_purchase_value = serializers.DecimalField(max_digits=20, decimal_places=2, allow_null=True)
    reconciled = serializers.BooleanField()
    lines = AllocationLineSerializer(many=True)


class ReversalLineSerializer(serializers.Serializer):
    sku = serializers.CharField()
    purchase = serializers.CharField(help_text="restored, closed or superseded_since.")
    landed = serializers.CharField(help_text="restored, closed or superseded_since.")


class ReversalSerializer(serializers.Serializer):
    reversal = BatchDetailSerializer()
    lines = ReversalLineSerializer(many=True)


# ── price master ───────────────────────────────────────────────────────────────────────────────────────────────────


class PriceMasterEntrySerializer(InternalFieldsMixin, serializers.Serializer):
    internal_fields = ("landed_unit_cost", "landed_version_key", "landed_effective_from")
    component = PricingComponentRefSerializer()
    purchase_price = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True, source="purchase.amount", default=None)
    purchase_version_key = serializers.CharField(allow_null=True, source="purchase.version_key", default=None)
    purchase_effective_from = serializers.DateField(allow_null=True, source="purchase.effective_from", default=None)
    landed_unit_cost = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True, source="landed.amount", default=None, required=False, help_text=INTERNAL_NOTE)
    landed_version_key = serializers.CharField(allow_null=True, source="landed.version_key", default=None, required=False, help_text=INTERNAL_NOTE)
    landed_effective_from = serializers.DateField(allow_null=True, source="landed.effective_from", default=None, required=False, help_text=INTERNAL_NOTE)
    supplier = PricingSupplierRefSerializer(allow_null=True)
    batch = BatchRefSerializer(allow_null=True)
    currency = serializers.CharField()
